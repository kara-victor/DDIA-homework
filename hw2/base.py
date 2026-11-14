from dataclasses import dataclass
from threading import Event


# Модель запроса
@dataclass(frozen=True)
class Request:
    payload: str = ""


# Модель ответа сервера
@dataclass(frozen=True)
class Response:
    status_code: int
    payload: str = ""


# Бюджеты выполнения запроса
@dataclass(frozen=True)
class Budget:
    latency_seconds: float
    subrequest_latency_seconds: float | None = None
    max_subrequests: int = 1
    max_4xx_errors: int | None = None
    max_5xx_errors: int | None = None
    max_timeouts: int | None = None


# Интерфейс отправки одного подзапроса
class Sender:
    def send(
        self,
        request: Request,
        cancel: Event
    ) -> Response:
        raise NotImplementedError


# Ошибка, полученная от сервера
class RequestFailed(Exception):
    def __init__(self, response: Response):
        self.response = response
        super().__init__(
            f"HTTP failed code = {response.status_code}"
        )


# Ошибка превышения времени ожидания
class RequestTimedOut(TimeoutError):
    pass


# Клиент для работы с одним сервером
def client(
    request: Request,
    sender: Sender,
    budget: Budget,
    strategy,
    **options
) -> Response:
    return strategy(request, sender, budget, **options)


# Клиент для работы с несколькими серверами
def multi_client(
    request: Request,
    senders: list[Sender],
    budget: Budget,
    strategy,
    **options
) -> Response:
    return strategy(request, senders, budget, **options)
