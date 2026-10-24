import os

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from prometheus_client import CollectorRegistry, Counter, Histogram

from time import perf_counter


class MetricsCollector:
    def __init__(self):
        self.registry = CollectorRegistry()

        self.requests_counter = Counter(
            "http_request_counter",
            "Total number of HTTP requests",
            [
                "deployment",
                "datacenter",
                "pod",
                "endpoint",
                "method",
                "status_code"
            ],
            registry=self.registry
        )

        self.errors_counter = Counter(
            "http_errors_total",
            "Total number of HTTP errors",
            [
                "deployment",
                "datacenter",
                "pod",
                "endpoint",
                "method",
                "status_code"
            ],
            registry=self.registry
        )

        self.request_duration = Histogram(
            "http_request_duration_seconds",
            "HTTP request duration in seconds",
            [
                "deployment",
                "datacenter",
                "pod",
                "endpoint",
                "method"
            ],
            registry=self.registry
        )

    def record_request(self, labels: dict, duration: float):
        self.requests_counter.labels(**labels).inc()

        status_code = int(labels["status_code"])

        if status_code >= 400:
            self.errors_counter.labels(**labels).inc()

        histogram_labels = labels.copy()
        histogram_labels.pop("status_code")

        self.request_duration.labels(**histogram_labels).observe(duration)


class MetricsMiddleware(BaseHTTPMiddleware):

    def __init__(self, app):
        super().__init__(app)
        self.collector = MetricsCollector()

        self.static_labels = {
            "deployment": os.getenv("DEPLOYMENT_NAME", "characters-service"),
            "datacenter": os.getenv("DATACENTER", "local"),
            "pod": os.getenv("POD_NAME", "pod-1")
        }

    async def dispatch(self, request: Request, call_next):
        request.state.metrics_registry = self.collector.registry

        excluded_paths = {
            "/health",
            "/metrics",
            "/docs",
            "/openapi.json",
            "/redoc"
        }

        if request.url.path in excluded_paths:
            return await call_next(request)

        start_time = perf_counter()
        status_code = 500

        try:
            response = await call_next(request)
            status_code = response.status_code
            return response

        finally:  # NEW
            duration = perf_counter() - start_time

            route = request.scope.get("route")

            if route is not None:
                endpoint = route.path
            elif status_code == 500:
                endpoint = "unknown"
            else:
                endpoint = None

            if endpoint is not None:
                labels = {
                    **self.static_labels,
                    "endpoint": endpoint,
                    "method": request.method,
                    "status_code": str(status_code)
                }

                self.collector.record_request(labels, duration)
