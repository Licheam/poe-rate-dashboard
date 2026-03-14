import json
import re
from typing import Any


TARGET_URL = "https://poe.com/leaderboard"
NAVIGATION_TIMEOUT_MS = 60_000
SETTLE_WAIT_MS = 8_000
POST_SCROLL_WAIT_MS = 2_000


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


def analyze_dom(page) -> dict[str, Any]:
    return page.evaluate(
        """
        () => {
            const normalize = (value) => (value || "").replace(/\\s+/g, " ").trim();

            const isVisible = (element) => {
                if (!element) return false;
                const style = window.getComputedStyle(element);
                if (style.display === "none" || style.visibility === "hidden") return false;
                const rect = element.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            };

            const simpleSelector = (element) => {
                if (!element || !element.tagName) return null;
                let selector = element.tagName.toLowerCase();
                if (element.id) {
                    return `${selector}#${element.id}`;
                }

                const classNames = Array.from(element.classList || [])
                    .filter(Boolean)
                    .slice(0, 3);
                if (classNames.length) {
                    selector += classNames.map((name) => `.${name}`).join("");
                }
                return selector;
            };

            const selectorPath = (element) => {
                if (!element || !element.tagName) return null;
                const parts = [];
                let current = element;

                while (current && current.nodeType === Node.ELEMENT_NODE && parts.length < 5) {
                    const selector = simpleSelector(current);
                    if (!selector) break;
                    parts.unshift(selector);
                    if (current.id) break;
                    current = current.parentElement;
                }

                return parts.join(" > ");
            };

            const getText = (element) => normalize(element ? element.textContent : "");
            const rankRegex = /(?:^|\\b)#?(\\d{1,3})(?:\\b|[.)])/;
            const shareRegex = /(\\d+(?:\\.\\d+)?)%/;
            const titleRegex = /(top\\s+(models|apps)|leaderboard|ranking|排行|榜单)/i;
            const noisyText = new Set([
                "weekly",
                "monthly",
                "daily",
                "leaderboard",
                "top models",
                "top apps",
                "market share",
                "share",
                "rank",
                "ranking",
                "models",
                "apps",
            ]);

            const majorElements = [];
            for (const selector of ["header", "nav", "main", "section", "footer", "h1", "h2", "h3"]) {
                const elements = Array.from(document.querySelectorAll(selector))
                    .filter(isVisible)
                    .slice(0, 12)
                    .map((element) => ({
                        selector: selectorPath(element),
                        tag: element.tagName.toLowerCase(),
                        text: getText(element).slice(0, 160),
                    }))
                    .filter((entry) => entry.text || ["main", "section", "header", "nav", "footer"].includes(entry.tag));
                majorElements.push(...elements);
            }

            const titleCandidates = Array.from(
                document.querySelectorAll("h1, h2, h3, h4, div, span, p")
            )
                .filter(isVisible)
                .map((element) => ({
                    text: getText(element),
                    selector: selectorPath(element),
                }))
                .filter((entry) => entry.text && titleRegex.test(entry.text))
                .slice(0, 20);

            const allElements = Array.from(document.querySelectorAll("body *"));
            let bestContainer = null;

            for (const element of allElements) {
                if (!isVisible(element)) continue;

                const directChildren = Array.from(element.children).filter(isVisible);
                if (directChildren.length < 3) continue;

                const rowCandidates = directChildren.filter((child) => {
                    const text = getText(child);
                    return shareRegex.test(text) || rankRegex.test(text);
                });

                if (rowCandidates.length < 3) continue;

                const text = getText(element);
                const score =
                    rowCandidates.length * 10 +
                    (text.match(/%/g) || []).length * 2 +
                    (text.match(/#/g) || []).length;

                if (!bestContainer || score > bestContainer.score) {
                    bestContainer = {
                        element,
                        score,
                        rowCandidates,
                    };
                }
            }

            const leaderboard = {
                found: false,
                containerSelector: null,
                rowSelector: null,
                rowCount: 0,
                detectedFields: {
                    rank: null,
                    model_name: null,
                    market_share: null,
                },
                samples: [],
            };

            if (bestContainer) {
                leaderboard.found = true;
                leaderboard.containerSelector = selectorPath(bestContainer.element);
                leaderboard.rowSelector = simpleSelector(bestContainer.rowCandidates[0]);
                leaderboard.rowCount = bestContainer.rowCandidates.length;

                const samples = bestContainer.rowCandidates.slice(0, 8).map((row) => {
                    const rowText = getText(row);
                    const rankMatch = rowText.match(rankRegex);
                    const shareMatch = rowText.match(shareRegex);
                    const links = Array.from(row.querySelectorAll("a"))
                        .filter(isVisible)
                        .map((link) => ({
                            text: getText(link),
                            href: link.getAttribute("href"),
                            selector: selectorPath(link),
                        }))
                        .filter((link) => link.text);

                    const leafTexts = Array.from(row.querySelectorAll("span, div, p, strong, b"))
                        .filter(isVisible)
                        .map((element) => ({
                            text: getText(element),
                            selector: selectorPath(element),
                        }))
                        .filter((entry) => entry.text && entry.text.length <= 80);

                    const modelCandidate =
                        links.find((entry) => !noisyText.has(entry.text.toLowerCase())) ||
                        leafTexts.find(
                            (entry) =>
                                !noisyText.has(entry.text.toLowerCase()) &&
                                !rankRegex.test(entry.text) &&
                                !shareRegex.test(entry.text)
                        ) ||
                        null;

                    const rankCandidate = leafTexts.find((entry) => rankRegex.test(entry.text)) || null;
                    const shareCandidate = leafTexts.find((entry) => shareRegex.test(entry.text)) || null;

                    return {
                        row_selector: selectorPath(row),
                        row_text: rowText.slice(0, 240),
                        rank: rankMatch ? rankMatch[1] : null,
                        market_share: shareMatch ? `${shareMatch[1]}%` : null,
                        model_name: modelCandidate ? modelCandidate.text : null,
                        fields: {
                            rank: rankCandidate ? rankCandidate.selector : null,
                            model_name: modelCandidate ? modelCandidate.selector : null,
                            market_share: shareCandidate ? shareCandidate.selector : null,
                        },
                        links: links.slice(0, 3),
                    };
                });

                leaderboard.samples = samples;

                const firstSample = samples.find(
                    (sample) => sample.fields.rank || sample.fields.model_name || sample.fields.market_share)
                    || null;

                if (firstSample) {
                    leaderboard.detectedFields = {
                        rank: firstSample.fields.rank,
                        model_name: firstSample.fields.model_name,
                        market_share: firstSample.fields.market_share,
                    };
                }
            }

            const scriptTags = Array.from(document.scripts).map((script) => ({
                src: script.src || null,
                type: script.type || null,
                is_inline: !script.src,
                text_preview: script.src ? null : normalize(script.textContent).slice(0, 200),
            }));

            return {
                title: document.title,
                url: window.location.href,
                majorElements,
                titleCandidates,
                hasTopModelsTitle: titleCandidates.some((entry) => /top\\s+models/i.test(entry.text)),
                hasLeaderboardLikeTitle: titleCandidates.length > 0,
                leaderboard,
                scriptTags,
            };
        }
        """
    )


def summarize_requests(logs: list[dict[str, Any]]) -> dict[str, Any]:
    interesting = []
    frontend_signals = {
        "api_or_data_requests": [],
        "graphql_requests": [],
        "frontend_rendering_likely": False,
    }

    for item in logs:
        url = item["url"]
        if not re.search(r"(api|graphql|gql|/_next|static|assets|chunk|js)", url, flags=re.IGNORECASE):
            continue

        entry = {
            "method": item["method"],
            "url": url,
            "resource_type": item["resource_type"],
            "status": item.get("status"),
            "content_type": item.get("content_type"),
        }
        interesting.append(entry)

        if re.search(r"(api|graphql|gql)", url, flags=re.IGNORECASE):
            frontend_signals["api_or_data_requests"].append(entry)
        if re.search(r"(graphql|gql)", url, flags=re.IGNORECASE):
            frontend_signals["graphql_requests"].append(entry)

    frontend_signals["frontend_rendering_likely"] = bool(
        frontend_signals["api_or_data_requests"] or frontend_signals["graphql_requests"]
    )

    return {
        "interesting_requests": interesting[:30],
        "frontend_signals": frontend_signals,
    }


def main() -> None:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    network_logs: list[dict[str, Any]] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        def handle_response(response) -> None:
            request = response.request
            network_logs.append(
                {
                    "url": request.url,
                    "method": request.method,
                    "resource_type": request.resource_type,
                    "status": response.status,
                    "content_type": response.headers.get("content-type"),
                    "body_preview": (extract_response_body(response) or "")[:500],
                }
            )

        context.on("response", handle_response)

        try:
            page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except PlaywrightTimeoutError:
                print("Network idle wait timed out; continuing with current DOM state.")
        except PlaywrightTimeoutError:
            print("Navigation timed out; continuing with partial page state.")

        page.wait_for_timeout(SETTLE_WAIT_MS)
        scroll_page(page)
        page.wait_for_timeout(POST_SCROLL_WAIT_MS)

        dom_report = analyze_dom(page)
        request_report = summarize_requests(network_logs)

        browser.close()

    output = {
        "page_title_and_major_elements": {
            "title": dom_report["title"],
            "url": dom_report["url"],
            "major_elements": dom_report["majorElements"],
        },
        "leaderboard_title_check": {
            "has_top_models_title": dom_report["hasTopModelsTitle"],
            "has_leaderboard_like_title": dom_report["hasLeaderboardLikeTitle"],
            "title_candidates": dom_report["titleCandidates"],
        },
        "leaderboard_dom_structure": dom_report["leaderboard"],
        "frontend_rendering_check": {
            "script_tag_count": len(dom_report["scriptTags"]),
            "script_tags": dom_report["scriptTags"][:20],
            "network": request_report,
        },
    }

    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
