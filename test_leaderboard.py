import json
import re
from collections.abc import Mapping


TARGET_URL = "https://poe.com/leaderboard"
WAIT_TIMEOUT_MS = 15_000
KEYWORDS = [
    "leaderboard",
    "weekly",
    "monthly",
    "rank",
    "ranking",
    "排名",
    "榜单",
]


def extract_payload(request) -> dict:
    post_data = request.post_data or ""
    parsed_payload = None

    if post_data:
        try:
            parsed_payload = request.post_data_json
        except Exception:
            parsed_payload = None

    if isinstance(parsed_payload, list):
        return {
            "batch": [
                {
                    "queryName": item.get("queryName"),
                    "variables": item.get("variables"),
                    "extensions": item.get("extensions"),
                    "raw": item,
                }
                for item in parsed_payload
                if isinstance(item, Mapping)
            ],
            "raw_body": post_data,
        }

    if isinstance(parsed_payload, Mapping):
        return {
            "queryName": parsed_payload.get("queryName"),
            "variables": parsed_payload.get("variables"),
            "extensions": parsed_payload.get("extensions"),
            "raw": parsed_payload,
            "raw_body": post_data,
        }

    return {
        "queryName": None,
        "variables": None,
        "extensions": None,
        "raw": None,
        "raw_body": post_data,
    }


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


def scroll_to_bottom(page) -> None:
    page.evaluate(
        """
        async () => {
            const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
            let previousHeight = -1;
            let stableRounds = 0;

            while (stableRounds < 3) {
                window.scrollTo(0, document.body.scrollHeight);
                await delay(800);

                const currentHeight = document.body.scrollHeight;
                if (currentHeight === previousHeight) {
                    stableRounds += 1;
                } else {
                    stableRounds = 0;
                    previousHeight = currentHeight;
                }
            }
        }
        """
    )


def click_tab(page, label: str) -> bool:
    patterns = [
        page.get_by_role("button", name=re.compile(label, re.IGNORECASE)),
        page.get_by_role("tab", name=re.compile(label, re.IGNORECASE)),
        page.get_by_text(re.compile(label, re.IGNORECASE)),
    ]

    for locator in patterns:
        try:
            target = locator.first
            target.wait_for(state="visible", timeout=5_000)
            target.click(timeout=5_000)
            print(f"Clicked tab/control: {label}")
            return True
        except Exception:
            continue

    print(f"Could not find clickable tab/control: {label}")
    return False


def print_html_snippets(html: str) -> None:
    normalized = re.sub(r"\s+", " ", html)
    snippets: list[dict] = []

    for keyword in KEYWORDS:
        for match in re.finditer(re.escape(keyword), normalized, flags=re.IGNORECASE):
            start = max(0, match.start() - 160)
            end = min(len(normalized), match.end() + 160)
            snippets.append(
                {
                    "keyword": keyword,
                    "snippet": normalized[start:end],
                }
            )

    print("=== HTML Keyword Snippets ===")
    if snippets:
        print(json.dumps(snippets, indent=2, ensure_ascii=False))
    else:
        print("No matching leaderboard-related keywords found in page HTML.")


def main() -> None:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    captured_logs: list[dict] = []
    request_index: dict[int, dict] = {}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        def handle_request(request) -> None:
            log_entry = {
                "url": request.url,
                "method": request.method,
                "headers": request.headers,
                "payload": extract_payload(request),
                "response": None,
            }
            request_index[id(request)] = log_entry
            captured_logs.append(log_entry)

        def handle_response(response) -> None:
            request = response.request
            log_entry = request_index.get(id(request))
            if log_entry is None:
                log_entry = {
                    "url": request.url,
                    "method": request.method,
                    "headers": request.headers,
                    "payload": extract_payload(request),
                    "response": None,
                }
                request_index[id(request)] = log_entry
                captured_logs.append(log_entry)

            log_entry["response"] = {
                "status": response.status,
                "status_text": response.status_text,
                "headers": response.headers,
                "content_type": response.headers.get("content-type"),
                "body": extract_response_body(response),
            }

        context.on("request", handle_request)
        context.on("response", handle_response)

        try:
            page.goto(TARGET_URL, wait_until="networkidle", timeout=60000)
        except PlaywrightTimeoutError:
            print("Navigation timed out after 60s; continuing with captured logs so far.")

        page.wait_for_timeout(WAIT_TIMEOUT_MS)
        scroll_to_bottom(page)
        page.wait_for_timeout(WAIT_TIMEOUT_MS)

        click_tab(page, "Weekly")
        page.wait_for_timeout(WAIT_TIMEOUT_MS)
        scroll_to_bottom(page)
        page.wait_for_timeout(WAIT_TIMEOUT_MS)

        click_tab(page, "Monthly")
        page.wait_for_timeout(WAIT_TIMEOUT_MS)
        scroll_to_bottom(page)
        page.wait_for_timeout(WAIT_TIMEOUT_MS)

        page_html = page.content()
        print_html_snippets(page_html)

        browser.close()

    print("=== Full Request/Response Logs ===")
    print(json.dumps(captured_logs, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
