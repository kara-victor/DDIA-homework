import json
import os

from urllib.request import Request, urlopen
from urllib.error import HTTPError


APP_URL = os.getenv("APP_URL", "http://localhost:8000")

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
EXCLUDED_PATHS = {"/health", "/metrics"}


def discover_endpoints():
    with urlopen(f"{APP_URL}/openapi.json", timeout=10) as response:
        openapi = json.load(response)

    endpoints = []

    for path, operations in openapi["paths"].items():
        if path in EXCLUDED_PATHS:
            continue

        for method in operations:
            if method in HTTP_METHODS:
                endpoints.append((method.upper(), path))

    return endpoints

def build_dashboard(endpoints):
    dashboard = {
        "id": None,
        "uid": "ddia-characters",
        "title": "DDIA - Characters Service",
        "editable": False,
        "timezone": "browser",
        "refresh": "10s",
        "time": {
            "from": "now-15m",
            "to": "now"
        },
        "panels": build_panels(endpoints),
        "templating": {
            "list": build_variables()
        }
    }

    return {
        "dashboard": dashboard,
        "overwrite": True
    }

def build_panels(endpoints):
    panels = []

    datasource = {
        "type": "prometheus",
        "uid": "victoriametrics"
    }

    percentiles = [0.50, 0.75, 0.90, 0.95, 0.99]

    for row, (method, path) in enumerate(endpoints):
        labels = (
            f'endpoint="{path}",'
            f'method="{method}",'
            'datacenter=~"$datacenter",'
            'pod=~"$pod"'
        )

        traffic_query = (
            f"sum(rate(http_request_counter_total"
            f"{{{labels}}}[$__rate_interval]))"
        )

        errors_query = (
            f"sum by (status_code) ("
            f"rate(http_errors_total{{{labels}}}[$__rate_interval]))"
        )

        bucket_query = (
            f"sum by (le) ("
            f"rate(http_request_duration_seconds_bucket"
            f"{{{labels}}}[$__rate_interval]))"
        )

        latency_targets = [
            {
                "refId": chr(65 + index),
                "expr": (
                    f"histogram_quantile({percentile}, "
                    f"{bucket_query})"
                ),
                "legendFormat": f"p{round(percentile * 100)}"
            }
            for index, percentile in enumerate(percentiles)
        ]

        panel_specs = [
            (
                "Traffic (RPS)",
                [{"refId": "A", "expr": traffic_query}],
                "reqps"
            ),
            (
                "Errors rate",
                [{
                    "refId": "A",
                    "expr": errors_query,
                    "legendFormat": "{{status_code}}"
                }],
                "reqps"
            ),
            (
                "Latency",
                latency_targets,
                "s"
            )
        ]

        for col, (title, targets, unit) in enumerate(panel_specs):
            panels.append({
                "id": row * 3 + col + 1,
                "title": f"{method} {path} — {title}",
                "type": "timeseries",
                "datasource": datasource,
                "targets": targets,
                "gridPos": {
                    "x": col * 8,
                    "y": row * 9,
                    "w": 8,
                    "h": 9
                },
                "fieldConfig": {
                    "defaults": {
                        "unit": unit
                    },
                    "overrides": []
                }
            })

    return panels

def build_variables():
    variables = []

    datasource = {
        "type": "prometheus",
        "uid": "victoriametrics"
    }

    for name in ("datacenter", "pod"):
        query = f"label_values(http_request_counter_total, {name})"

        variables.append({
            "name": name,
            "type": "query",
            "datasource": datasource,
            "definition": query,
            "query": {
                "qryType": 1,
                "query": query,
                "refId": "PrometheusVariableQueryEditor-VariableQuery"
            },
            "refresh": 1,
            "sort": 1,
            "multi": True,
            "includeAll": True,
            "allValue": ".*",
            "current": {
                "text": "All",
                "value": "$__all"
            },
            "options": []
        })

    return variables

def upload_dashboard(payload):
    grafana_url = os.getenv(
        "GRAFANA_URL",
        "http://grafana:3000"
    )

    token = os.environ["GRAFANA_SERVICE_ACCOUNT_TOKEN"]

    request = Request(
        url=f"{grafana_url}/api/dashboards/db",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        },
        method="POST"
    )

    try:
        with urlopen(request, timeout=15) as response:
            result = json.load(response)

    except HTTPError as error:
        print(f"Grafana API error: HTTP {error.code}")
        print(error.read().decode("utf-8"))
        raise

    return result

if __name__ == "__main__":
    endpoints = discover_endpoints()

    dashboard_payload = build_dashboard(endpoints)

    result = upload_dashboard(dashboard_payload)

    print(f"Discovered endpoints: {len(endpoints)}")
    print(
        f"Generated panels: "
        f"{len(dashboard_payload['dashboard']['panels'])}"
    )

    print(f"Dashboard created: {result['url']}")