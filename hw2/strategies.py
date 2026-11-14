import time
from collections import deque
from queue import Empty, Queue
from threading import Event, Thread

from base import Budget, Request, RequestFailed, RequestTimedOut, Response, Sender

# Выполнить один подзапрос с таймаутом
def _send_once(request: Request, sender: Sender, timeout: float) -> Response:
    cancel = Event()
    results: Queue[tuple[Response | None, Exception | None]] = Queue(maxsize=1)

    def worker() -> None:
        try:
            results.put((sender.send(request, cancel), None))
        except Exception as error:
            results.put((None, error))

    Thread(target=worker, daemon=True).start()

    try:
        response, error = results.get(timeout=timeout)
    except Empty as exc:
        cancel.set()
        raise RequestTimedOut("Request latency budget exhausted") from exc

    if error is not None:
        raise error
    if response is None:
        raise RuntimeError("Timeout")
    return response

# Одна попытка
def default(request: Request, sender: Sender, budget: Budget) -> Response:
    timeout = budget.latency_seconds
    if budget.subrequest_latency_seconds is not None:
        timeout = min(timeout, budget.subrequest_latency_seconds)

    response = _send_once(request, sender, timeout)
    if 200 <= response.status_code < 300:
        return response
    raise RequestFailed(response)

# Общие повторы, бюджеты и выбор отправителя
def _sequential(
    request: Request,
    senders: list[Sender],
    budget: Budget,
    backoff_seconds: float | None = None,
    choose=None,
    record=None,
) -> Response:
    if not senders:
        raise ValueError("No senders")

    deadline = time.monotonic() + budget.latency_seconds
    errors_4xx = errors_5xx = timeouts = 0
    last_error: Exception = RequestTimedOut("Request latency budget exhausted")

    for attempt in range(budget.max_subrequests):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RequestTimedOut("Request latency budget exhausted")

        index = choose(senders, deadline) if choose else attempt % len(senders)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RequestTimedOut("Request latency budget exhausted")

        if budget.subrequest_latency_seconds is not None:
            remaining = min(remaining, budget.subrequest_latency_seconds)

        response = None
        try:
            response = _send_once(request, senders[index], remaining)
        except RequestTimedOut as error:
            timeouts += 1
            last_error = error
            budget_used = budget.max_timeouts is not None and timeouts >= budget.max_timeouts
            needs_backoff = True
        else:
            if 200 <= response.status_code < 300:
                if record is not None:
                    record(index, False)
                return response

            last_error = RequestFailed(response)
            if 400 <= response.status_code < 500:
                errors_4xx += 1
                budget_used = (
                    budget.max_4xx_errors is not None
                    and errors_4xx >= budget.max_4xx_errors
                )
                needs_backoff = False
            else:
                errors_5xx += 1
                budget_used = (
                    budget.max_5xx_errors is not None
                    and errors_5xx >= budget.max_5xx_errors
                )
                needs_backoff = True

        if record is not None:
            record(index, True)

        if budget_used:
            raise last_error

        if backoff_seconds is not None and needs_backoff and attempt + 1 < budget.max_subrequests:
            delay = backoff_seconds * 2**attempt
            time.sleep(min(delay, max(0, deadline - time.monotonic())))

    raise last_error


def retries(request: Request, sender: Sender, budget: Budget) -> Response:
    return _sequential(request, [sender], budget)

# Как retries, но с растущей задержкой после 5xx и Timeout
def exponential_backoff(
    request: Request,
    sender: Sender,
    budget: Budget,
    base_delay_seconds: float = 0.05,
) -> Response:
    return _sequential(request, [sender], budget, backoff_seconds=base_delay_seconds)

# Для каждой новой попытки выбирать следующего отправителя по кругу
def round_robin(request: Request, senders: list[Sender], budget: Budget) -> Response:
    return _sequential(request, senders, budget)

# Запустить резервные запросы, если первый не успел успешно завершиться
def hedging(
    request: Request,
    senders: list[Sender],
    budget: Budget,
    hedging_delay: float = 0.05,
) -> Response:
    if not senders:
        raise ValueError("No senders")

    count = min(len(senders), budget.max_subrequests)
    if count <= 0:
        raise RequestTimedOut("Subrequest budget exhausted")

    deadline = time.monotonic() + budget.latency_seconds
    results: Queue[tuple[int, Response | None, float]] = Queue()
    cancellations: list[Event] = []
    subrequest_deadlines: list[float] = []

    def launch(index: int) -> None:
        cancel = Event()
        cancellations.append(cancel)
        started = time.monotonic()
        subrequest_deadlines.append(
            min(deadline, started + budget.subrequest_latency_seconds)
            if budget.subrequest_latency_seconds is not None
            else deadline
        )

        def worker() -> None:
            try:
                response = senders[index].send(request, cancel)
            except Exception:
                response = None
            results.put((index, response, time.monotonic()))

        Thread(target=worker, daemon=True).start()

    def wait_for_success(until: float) -> Response | None:
        while time.monotonic() < until:
            now = time.monotonic()

            for cancel, sub_deadline in zip(cancellations, subrequest_deadlines):
                if now >= sub_deadline:
                    cancel.set()

            next_deadline = min(
                (d for d in subrequest_deadlines if d > now),
                default=until,
            )
            wait_time = max(0, min(until, next_deadline) - now)

            try:
                index, response, finished_at = results.get(timeout=wait_time)
            except Empty:
                continue

            if response is not None and 200 <= response.status_code < 300 and finished_at <= subrequest_deadlines[index]:
                return response

        return None

    try:
        launch(0)
        first_wait = min(deadline, time.monotonic() + max(0, hedging_delay))
        response = wait_for_success(first_wait)
        if response is not None:
            return response

        for index in range(1, count):
            if time.monotonic() >= deadline:
                break
            launch(index)

        response = wait_for_success(deadline)
        if response is not None:
            return response

        raise RequestTimedOut("Request latency budget exhausted")
    finally:
        for cancel in cancellations:
            cancel.set()


# Создать функцию round-robin с памятью об ошибках каждого сервера
def circuit_breaker(
    window_size: int = 3,
    error_threshold: float = 0.5,
    cooldown_seconds: float = 0.2,
):
    history: dict[int, deque[bool]] = {}
    blocked_until: dict[int, float] = {}
    cursor = 0

    def choose(senders: list[Sender], deadline: float) -> int:
        nonlocal cursor
        while time.monotonic() < deadline:
            for _ in senders:
                index = cursor % len(senders)
                cursor += 1
                if time.monotonic() >= blocked_until.get(index, 0):
                    return index

            # Если все серверы отключены, ждём ближайшего восстановления.
            delay = min(blocked_until.values()) - time.monotonic()
            time.sleep(min(max(0, delay), max(0, deadline - time.monotonic())))

        raise RequestTimedOut("Request latency budget exhausted")

    def record(index: int, failed: bool) -> None:
        recent = history.setdefault(index, deque(maxlen=window_size))
        recent.append(failed)
        if len(recent) == window_size and sum(recent) / window_size > error_threshold:
            blocked_until[index] = time.monotonic() + cooldown_seconds
            recent.clear()

    def execute(request: Request, senders: list[Sender], budget: Budget) -> Response:
        return _sequential(request, senders, budget, choose=choose, record=record)

    return execute
