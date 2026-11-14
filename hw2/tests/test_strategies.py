import time
import unittest
from threading import Event, Lock
from unittest.mock import call, patch

from base import (
    Budget, Request, RequestFailed, RequestTimedOut, Response, Sender, client, multi_client
)
from strategies import (
    default, retries, exponential_backoff, round_robin, hedging, circuit_breaker
)


class FakeSender(Sender):
    def __init__(
        self,
        responses: list[Response],
        delay: float = 0,
        delays: list[float] | None = None,
    ) -> None:
        self.responses = responses
        self.delay = delay
        self.delays = delays
        self.calls = 0
        self.cancelled = Event()
        self._lock = Lock()

    def send(self, request: Request, cancel: Event) -> Response:
        with self._lock:
            index = self.calls
            self.calls += 1

        delay = self.delays[min(index, len(self.delays) - 1)] if self.delays else self.delay
        if cancel.wait(delay):
            self.cancelled.set()
            raise RuntimeError("Sender cancelled")
        return self.responses[min(index, len(self.responses) - 1)]


REQUEST = Request(payload="request-data")


class DefaultTests(unittest.TestCase):
    def test_2xx(self):
        sender = FakeSender([Response(200, "ok")])
        self.assertEqual(client(REQUEST, sender, Budget(1), default).status_code, 200)
        self.assertEqual(sender.calls, 1)

    def test_4xx(self):
        sender = FakeSender([Response(404)])
        with self.assertRaises(RequestFailed) as exc:
            client(REQUEST, sender, Budget(1), default)
        self.assertEqual(exc.exception.response.status_code, 404)
        self.assertEqual(sender.calls, 1)

    def test_5xx_without_retry(self):
        sender = FakeSender([Response(503), Response(200)])
        with self.assertRaises(RequestFailed):
            client(REQUEST, sender, Budget(1), default)
        self.assertEqual(sender.calls, 1)

    def test_total_timeout_cancels_sender(self):
        sender = FakeSender([Response(200)], delay=1)
        with self.assertRaises(RequestTimedOut):
            client(REQUEST, sender, Budget(0.02), default)
        self.assertTrue(sender.cancelled.wait(0.3))
        self.assertEqual(sender.calls, 1)

    def test_subrequest_timeout(self):
        sender = FakeSender([Response(200)], delay=1)
        with self.assertRaises(RequestTimedOut):
            client(REQUEST, sender, Budget(1, subrequest_latency_seconds=0.02), default)
        self.assertTrue(sender.cancelled.wait(0.3))


class RetriesTests(unittest.TestCase):
    def test_eventual_success(self):
        sender = FakeSender([Response(503), Response(200)])
        result = client(REQUEST, sender, Budget(1, max_subrequests=3), retries)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(sender.calls, 2)

    def test_4xx_budget(self):
        sender = FakeSender([Response(404), Response(200)])
        with self.assertRaises(RequestFailed):
            client(REQUEST, sender, Budget(1, max_subrequests=3, max_4xx_errors=1), retries)
        self.assertEqual(sender.calls, 1)

    def test_5xx_budget(self):
        sender = FakeSender([Response(503), Response(503), Response(200)])
        with self.assertRaises(RequestFailed):
            client(REQUEST, sender, Budget(1, max_subrequests=4, max_5xx_errors=2), retries)
        self.assertEqual(sender.calls, 2)

    def test_timeout_budget(self):
        sender = FakeSender([Response(200)], delay=1)
        with self.assertRaises(RequestTimedOut):
            client(
                REQUEST, sender,
                Budget(1, subrequest_latency_seconds=0.02, max_subrequests=3, max_timeouts=1),
                retries,
            )
        self.assertEqual(sender.calls, 1)
        self.assertTrue(sender.cancelled.wait(0.3))

    def test_max_subrequests(self):
        sender = FakeSender([Response(503), Response(503), Response(200)])
        with self.assertRaises(RequestFailed):
            client(REQUEST, sender, Budget(1, max_subrequests=2), retries)
        self.assertEqual(sender.calls, 2)


class BackoffTests(unittest.TestCase):
    def test_exponential_delays_after_5xx(self):
        sender = FakeSender([Response(503), Response(503), Response(200)])
        with patch("strategies.time.sleep") as sleeping:
            answer = client(
                REQUEST, sender, Budget(10, max_subrequests=3),
                exponential_backoff, base_delay_seconds=0.1,
            )
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(sleeping.call_args_list, [call(0.1), call(0.2)])

    def test_4xx_not_delayed(self):
        sender = FakeSender([Response(404), Response(200)])
        with patch("strategies.time.sleep") as sleeping:
            answer = client(
                REQUEST, sender, Budget(10, max_subrequests=2),
                exponential_backoff, base_delay_seconds=0.1,
            )
        self.assertEqual(answer.status_code, 200)
        sleeping.assert_not_called()

    def test_timeout_triggers_delay(self):
        sender = FakeSender([Response(200), Response(200)], delays=[1, 0])
        with patch("strategies.time.sleep") as sleeping:
            result = client(
                REQUEST, sender,
                Budget(1, subrequest_latency_seconds=0.02, max_subrequests=2),
                exponential_backoff, base_delay_seconds=0.1,
            )
        self.assertEqual(result.status_code, 200)
        sleeping.assert_called_once_with(0.1)
        self.assertTrue(sender.cancelled.wait(0.3))

    def test_delay_respects_total_budget(self):
        sender = FakeSender([Response(503)])
        with self.assertRaises(RequestTimedOut):
            client(
                REQUEST, sender, Budget(0.03, max_subrequests=10),
                exponential_backoff, base_delay_seconds=0.1,
            )
        self.assertEqual(sender.calls, 1)


class RoundRobinTests(unittest.TestCase):
    def test_rotates_between_senders(self):
        senders = [
            FakeSender([Response(503)]),
            FakeSender([Response(503)]),
            FakeSender([Response(200)]),
        ]
        result = multi_client(REQUEST, senders, Budget(1, max_subrequests=3), round_robin)
        self.assertEqual(result.status_code, 200)
        self.assertEqual([s.calls for s in senders], [1, 1, 1])

    def test_wraps_around(self):
        senders = [FakeSender([Response(503), Response(200)]), FakeSender([Response(503)])]
        result = multi_client(REQUEST, senders, Budget(1, max_subrequests=3), round_robin)
        self.assertEqual(result.status_code, 200)
        self.assertEqual([s.calls for s in senders], [2, 1])

    def test_first_success_stops(self):
        senders = [FakeSender([Response(200)]), FakeSender([Response(200)])]
        multi_client(REQUEST, senders, Budget(1, max_subrequests=5), round_robin)
        self.assertEqual([s.calls for s in senders], [1, 0])

    def test_timeout_moves_to_next_sender(self):
        slow = FakeSender([Response(200)], delay=1)
        fast = FakeSender([Response(200)])
        result = multi_client(
            REQUEST, [slow, fast],
            Budget(1, subrequest_latency_seconds=0.02, max_subrequests=2),
            round_robin,
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual([slow.calls, fast.calls], [1, 1])
        self.assertTrue(slow.cancelled.wait(0.3))


class HedgingTests(unittest.TestCase):
    def test_fast_first_does_not_launch_other_requests(self):
        senders = [FakeSender([Response(200)]), FakeSender([Response(200)])]
        result = multi_client(
            REQUEST, senders, Budget(0.3), hedging, hedging_delay=0.05,
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual([s.calls for s in senders], [1, 0])

    def test_slow_first_launches_parallel_request(self):
        slow = FakeSender([Response(200)], delay=1)
        fast = FakeSender([Response(200)])
        result = multi_client(
            REQUEST, [slow, fast], Budget(0.3), hedging, hedging_delay=0.02,
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual([slow.calls, fast.calls], [1, 1])
        self.assertTrue(slow.cancelled.wait(0.3))

    def test_early_error_launches_other_sender(self):
        bad = FakeSender([Response(503)])
        good = FakeSender([Response(200)])
        result = multi_client(
            REQUEST, [bad, good], Budget(0.3), hedging, hedging_delay=0.1,
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual([bad.calls, good.calls], [1, 1])

    def test_all_errors_finish_at_latency_budget(self):
        senders = [FakeSender([Response(503)]), FakeSender([Response(404)])]
        with self.assertRaises(RequestTimedOut):
            multi_client(
                REQUEST, senders, Budget(0.03), hedging, hedging_delay=0.005,
            )
        self.assertEqual([s.calls for s in senders], [1, 1])

    def test_all_slow_senders_are_cancelled(self):
        senders = [FakeSender([Response(200)], delay=1) for _ in range(3)]
        with self.assertRaises(RequestTimedOut):
            multi_client(
                REQUEST, senders, Budget(0.05), hedging, hedging_delay=0.01,
            )
        self.assertEqual([s.calls for s in senders], [1, 1, 1])
        self.assertTrue(all(s.cancelled.wait(0.3) for s in senders))


class CircuitBreakerTests(unittest.TestCase):
    def test_round_robin_works_while_servers_healthy(self):
        strategy = circuit_breaker()
        budget = Budget(0.5)
        for expected in [0, 1, 0]:
            senders = [FakeSender([Response(200)]), FakeSender([Response(200)])]
            result = multi_client(REQUEST, senders, budget, strategy)
            self.assertEqual(result.status_code, 200)
            self.assertEqual([s.calls for s in senders], [int(i == expected) for i in range(2)])

    def test_unhealthy_server_removed(self):
        strategy = circuit_breaker(window_size=2, error_threshold=0.5, cooldown_seconds=0.3)
        budget = Budget(0.5, max_subrequests=2)
        for _ in range(2):
            senders = [FakeSender([Response(503)]), FakeSender([Response(200)])]
            self.assertEqual(multi_client(REQUEST, senders, budget, strategy).status_code, 200)

        senders = [FakeSender([Response(503)]), FakeSender([Response(200)])]
        self.assertEqual(multi_client(REQUEST, senders, budget, strategy).status_code, 200)
        self.assertEqual([s.calls for s in senders], [0, 1])

    def test_server_returns_after_cooldown(self):
        strategy = circuit_breaker(window_size=1, error_threshold=0, cooldown_seconds=0.04)
        budget = Budget(0.5, max_subrequests=2)
        senders = [FakeSender([Response(503)]), FakeSender([Response(200)])]
        self.assertEqual(multi_client(REQUEST, senders, budget, strategy).status_code, 200)
        time.sleep(0.06)

        senders = [FakeSender([Response(200)]), FakeSender([Response(200)])]
        self.assertEqual(multi_client(REQUEST, senders, budget, strategy).status_code, 200)
        self.assertEqual([s.calls for s in senders], [1, 0])

    def test_all_blocked_stops_at_latency_budget(self):
        strategy = circuit_breaker(window_size=1, error_threshold=0, cooldown_seconds=0.3)
        senders = [FakeSender([Response(503)]), FakeSender([Response(503)])]
        with self.assertRaises(RequestFailed):
            multi_client(REQUEST, senders, Budget(0.5, max_subrequests=2), strategy)

        senders = [FakeSender([Response(200)]), FakeSender([Response(200)])]
        with self.assertRaises(RequestTimedOut):
            multi_client(REQUEST, senders, Budget(0.02), strategy)
        self.assertEqual([s.calls for s in senders], [0, 0])


if __name__ == "__main__":
    unittest.main()
