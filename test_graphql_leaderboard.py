import json
import re
import sys
from dataclasses import dataclass
from typing import Any


TARGET_URL = "https://poe.com/leaderboard"
GRAPHQL_URL = "https://poe.com/api/gql_POST"
NAVIGATION_TIMEOUT_MS = 60_000
SETTLE_WAIT_MS = 8_000
POST_SCROLL_WAIT_MS = 2_000


@dataclass
class GraphQLRecord:
    method: str
    url: str
    status: int | None
    request_headers: dict[str, str]
    request_body: str | None
    response_headers: dict[str, str]
    response_body: str | None

    def to_summary(self) -> dict[str, Any]:
        payload = parse_json(self.request_body)
        response = parse_json(self.response_body)
        return {
            "method": self.method,
            "url": self.url,
            "status": self.status,
            "query_name": payload.get("queryName") if isinstance(payload, dict) else None,
            "variables": payload.get("variables") if isinstance(payload, dict) else None,
            "extensions": payload.get("extensions") if isinstance(payload, dict) else None,
            "response_top_level_keys": sorted(response.keys()) if isinstance(response, dict) else None,
            "response_data_keys": sorted(response.get("data", {}).keys()) if isinstance(response, dict) and isinstance(response.get("data"), dict) else None,
        }


def parse_json(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def extract_response_body(response) -> str | None:
    try:
        body = response.body()
    except Exception as exc:
        return f"<<failed to read body: {exc}>>"

    if body is None:
        return None

    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("utf-8", errors="replace")


def scroll_page(page) -> None:
    page.evaluate(
        """
        async () => {
            const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
            const steps = 6;
            const maxY = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);

            for (let index = 1; index <= steps; index += 1) {
                window.scrollTo(0, Math.floor((maxY * index) / steps));
                await delay(500);
            }

            window.scrollTo(0, 0);
        }
        """
    )


def classify_leaderboard_candidate(payload: dict[str, Any] | None, response: dict[str, Any] | None) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if not isinstance(payload, dict) or not isinstance(response, dict):
        return False, reasons

    query_name = str(payload.get("queryName") or "")
    variables_json = json.dumps(payload.get("variables", {}), ensure_ascii=False).lower()
    response_json = json.dumps(response, ensure_ascii=False).lower()

    if re.search(r"leaderboard|rank|top", query_name, flags=re.IGNORECASE):
        reasons.append(f"queryName={query_name}")
    if re.search(r"leaderboard|rank|market.?share|top.?models|top.?apps", variables_json, flags=re.IGNORECASE):
        reasons.append("variables contain leaderboard-like terms")
    if re.search(r"leaderboard|rank|market.?share|top.?models|top.?apps", response_json, flags=re.IGNORECASE):
        reasons.append("response contains leaderboard-like terms")

    data = response.get("data")
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, dict) and any(k in value for k in ("edges", "nodes", "items", "results")):
                reasons.append(f"data.{key} looks like a collection payload")
                break
            if isinstance(value, list):
                reasons.append(f"data.{key} is a list")
                break

    return bool(reasons), reasons


def replay_request(record: GraphQLRecord) -> dict[str, Any]:
    try:
        import httpx
    except ImportError:
        return {"ok": False, "error": "httpx is not installed"}

    payload = parse_json(record.request_body)
    if not isinstance(payload, dict):
        return {"ok": False, "error": "request body is not valid JSON"}

    headers = {}
    for key, value in record.request_headers.items():
        lowered = key.lower()
        if lowered in {
            "accept",
            "content-type",
            "origin",
            "poe-formkey",
            "poe-queryname",
            "poe-revision",
            "poegraphql",
            "referer",
            "user-agent",
            "cookie",
        }:
            headers[key] = value

    try:
        response = httpx.post(
            GRAPHQL_URL,
            headers=headers,
            json=payload,
            timeout=30,
        )
    except Exception as exc:
        return {"ok": False, "error": f"replay request failed: {exc}"}

    parsed = parse_json(response.text)
    return {
        "ok": response.is_success,
        "status_code": response.status_code,
        "response_headers": dict(response.headers),
        "response_json": parsed,
        "response_text_preview": response.text[:1000],
    }


def run_browser_capture() -> dict[str, Any]:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {
            "ok": False,
            "error": "playwright is not installed in the current environment",
        }

    graphql_records: list[GraphQLRecord] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        def handle_response(response) -> None:
            request = response.request
            if request.method != "POST" or request.url != GRAPHQL_URL:
                return

            try:
                request_body = request.post_data
            except Exception as exc:
                request_body = f"<<failed to read request body: {exc}>>"

            graphql_records.append(
                GraphQLRecord(
                    method=request.method,
                    url=request.url,
                    status=response.status,
                    request_headers=request.headers,
                    request_body=request_body,
                    response_headers=response.headers,
                    response_body=extract_response_body(response),
                )
            )

        context.on("response", handle_response)

        try:
            page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(SETTLE_WAIT_MS)
            scroll_page(page)
            page.wait_for_timeout(POST_SCROLL_WAIT_MS)
        except Exception as exc:
            browser.close()
            return {
                "ok": False,
                "error": f"failed to load {TARGET_URL}: {exc}",
            }

        browser.close()

    summaries = [record.to_summary() for record in graphql_records]
    candidates = []
    for index, record in enumerate(graphql_records):
        payload = parse_json(record.request_body)
        response = parse_json(record.response_body)
        matched, reasons = classify_leaderboard_candidate(payload, response)
        if matched:
            candidates.append(
                {
                    "index": index,
                    "reasons": reasons,
                    "request": payload,
                    "response_preview": response,
                }
            )

    replay = replay_request(graphql_records[candidates[0]["index"]]) if candidates else None
    return {
        "ok": True,
        "target_url": TARGET_URL,
        "graphql_request_count": len(graphql_records),
        "graphql_requests": summaries,
        "leaderboard_candidates": candidates,
        "replay_result": replay,
    }


def run_connectivity_check() -> dict[str, Any]:
    try:
        import httpx
    except ImportError:
        return {"ok": False, "error": "httpx is not installed"}

    try:
        response = httpx.get(TARGET_URL, headers={"user-agent": "Mozilla/5.0"}, timeout=15)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    return {
        "ok": response.is_success,
        "status_code": response.status_code,
        "content_type": response.headers.get("content-type"),
        "text_preview": response.text[:300],
    }


def main() -> int:
    result = run_browser_capture()
    result["environment"] = {
        "python": sys.version,
        "connectivity_check": run_connectivity_check(),
        "known_graphql_shape": {
            "endpoint": GRAPHQL_URL,
            "request_json_keys": ["queryName", "variables", "extensions.hash"],
            "example_seen_in_repo": {
                "queryName": "RateCardModalQuery",
                "variables": {"botId": 0},
                "extensions": {"hash": "<persisted-query-hash>"},
            },
        },
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
