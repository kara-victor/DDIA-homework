import json
import os
import random
import time

from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


APP_URL = os.getenv("APP_URL", "http://app:8000")

CHARACTER = {
    "name": "Тест Тестович",
    "level": 1,
    "character_class": "warrior",
    "character_type": "human",
    "description": "тест    ",
    "sex": "male",
}


def send_request(method, path, data=None):
    body = json.dumps(data).encode("utf-8") if data is not None else None

    request = Request(
        f"{APP_URL}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method=method,
    )

    try:
        with urlopen(request, timeout=5) as response:
            content = response.read()
            return response.status, json.loads(content) if content else None

    except HTTPError as error:
        return error.code, None


def generate_load():
    character_id = None

    while True:
        scenarios = [
            ("POST", "/characters", CHARACTER),

            # HTTP 400
            ("POST", "/characters", {**CHARACTER, "level": -1}),

            # HTTP 422
            ("POST", "/characters", {}),

            # HTTP 404
            ("GET", "/characters/999999999", None),

            # HTTP 404
            ("PUT", "/characters/999999999", CHARACTER),
        ]

        weights = [1, 2, 2, 2, 2]

        if character_id is not None:
            scenarios.extend([
                ("GET", f"/characters/{character_id}", None),
                ("PUT", f"/characters/{character_id}", CHARACTER),

                # HTTP 400
                ("PUT", f"/characters/{character_id}",
                 {**CHARACTER, "level": -1}),

                # HTTP 422
                ("PUT", f"/characters/{character_id}", {}),
            ])

            weights.extend([4, 4, 2, 2])

        burst_size = random.randint(2, 10)

        for _ in range(burst_size):
            method, path, data = random.choices(
                scenarios,
                weights=weights,
                k=1
            )[0]

            try:
                status, result = send_request(method, path, data)

                print(f"{method} {path} -> {status}", flush=True)

                if method == "POST" and status == 201:
                    if isinstance(result, dict):
                        new_id = result.get(
                            "id",
                            result.get("character_id")
                        )

                        if new_id is not None:
                            character_id = new_id

                if (
                    status == 404
                    and character_id is not None
                    and path == f"/characters/{character_id}"
                ):
                    character_id = None

            except (URLError, TimeoutError, json.JSONDecodeError) as error:
                print(f"Request error: {error}", flush=True)

            time.sleep(random.uniform(0.05, 0.5))

        time.sleep(random.uniform(0.3, 3.0))


if __name__ == "__main__":
    generate_load()