import json
import logging

import httpx
from fastapi import HTTPException

from services.rate_parser import (
    extract_bot_id,
    extract_redirect_handle,
    normalize_handle_case,
    parse_rate_markdown,
)


logger = logging.getLogger("poe_rate_dashboard")

POE_GRAPHQL_URL = "https://poe.com/api/gql_POST"
LEADERBOARD_TYPE_TITLES = {
    "models": "Top Models",
    "apps": "Top Apps",
}
POE_LEADERBOARD_QUERY_NAME = "LeaderboardPageColumn_LeaderboardStatsQuery"
POE_LEADERBOARD_QUERY_HASH = "803d5007cefa51acd05611b9e5acec267561ec5f890e55f8e19589ccf7583511"
POE_LEADERBOARD_INTERVAL = "week"

HEADERS = {
    "accept": "*/*",
    "content-type": "application/json",
    "origin": "https://poe.com",
    "poe-formkey": "63c529af0ecc4d2491c4525e4b1fbf6b",
    "poe-queryname": "RateCardModalQuery",
    "poe-revision": "3841a92fa633db990ddb39cc8bb28cb528659f45",
    "poegraphql": "1",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
}


def _truncate_for_log(value, limit=2000):
    if value is None:
        return None

    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...<truncated {len(text) - limit} chars>"


def _extract_leaderboard_handle(ranked):
    if not isinstance(ranked, dict):
        return None, "ranked is not a dict"

    candidates = [
        ("displayName", ranked.get("displayName")),
        ("handle", ranked.get("handle")),
        ("username", ranked.get("username")),
        ("slug", ranked.get("slug")),
        ("name", ranked.get("name")),
        ("bot.displayName", ranked.get("bot", {}).get("displayName") if isinstance(ranked.get("bot"), dict) else None),
        ("bot.handle", ranked.get("bot", {}).get("handle") if isinstance(ranked.get("bot"), dict) else None),
    ]
    for source, raw in candidates:
        if raw is None:
            continue
        handle = normalize_handle_case(str(raw).strip())
        if handle:
            return handle, source

    return None, f"no supported handle field in ranked keys={sorted(ranked.keys())}"


def validate_leaderboard_type(value):
    leaderboard_type = (value or "models").strip().lower()
    if leaderboard_type not in LEADERBOARD_TYPE_TITLES:
        raise HTTPException(status_code=422, detail='type must be "models" or "apps"')
    return leaderboard_type


async def fetch_poe_leaderboard_via_graphql(count: int, type: str = "models"):
    leaderboard_type = validate_leaderboard_type(type)
    payload = {
        "queryName": POE_LEADERBOARD_QUERY_NAME,
        "variables": {"interval": POE_LEADERBOARD_INTERVAL},
        "extensions": {"hash": POE_LEADERBOARD_QUERY_HASH},
    }
    headers = {
        **HEADERS,
        "poe-queryname": POE_LEADERBOARD_QUERY_NAME,
    }

    logger.info(
        "Fetching Poe leaderboard via GraphQL: type=%s count=%s url=%s headers=%s payload=%s",
        leaderboard_type,
        count,
        POE_GRAPHQL_URL,
        _truncate_for_log({k: v for k, v in headers.items() if k.lower() != "cookie"}),
        _truncate_for_log(payload),
    )

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.post(
                POE_GRAPHQL_URL,
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            logger.info(
                "Poe leaderboard GraphQL response received: status=%s headers=%s body=%s",
                resp.status_code,
                _truncate_for_log(dict(resp.headers)),
                _truncate_for_log(data),
            )
    except httpx.HTTPStatusError as exc:
        try:
            response_text = exc.response.text
        except Exception:
            response_text = "<<unable to read response body>>"
        logger.exception(
            "Poe leaderboard GraphQL HTTP status error: status=%s body=%s",
            exc.response.status_code,
            _truncate_for_log(response_text),
        )
        raise HTTPException(
            status_code=502,
            detail=f"Poe leaderboard GraphQL request failed with status {exc.response.status_code}",
        ) from exc
    except httpx.HTTPError as exc:
        logger.exception("Poe leaderboard GraphQL network error")
        raise HTTPException(status_code=502, detail="Failed to fetch Poe leaderboard via GraphQL") from exc
    except json.JSONDecodeError as exc:
        logger.exception("Poe leaderboard GraphQL returned invalid JSON")
        raise HTTPException(status_code=502, detail="Poe leaderboard GraphQL returned invalid JSON") from exc

    if data.get("errors"):
        logger.error("Poe leaderboard GraphQL returned errors: %s", _truncate_for_log(data.get("errors")))
        raise HTTPException(status_code=502, detail="Poe leaderboard GraphQL returned errors")

    ranking_key = "topModelLatest" if leaderboard_type == "models" else "topAppLatest"
    data_root = data.get("data", {})
    rankings_parent = data_root.get(ranking_key, {})
    rankings = rankings_parent.get("topRankings")
    logger.info(
        "Parsing leaderboard response: ranking_key=%s data_keys=%s parent_keys=%s rankings_type=%s rankings_len=%s",
        ranking_key,
        sorted(data_root.keys()) if isinstance(data_root, dict) else data_root.__class__.__name__,
        sorted(rankings_parent.keys()) if isinstance(rankings_parent, dict) else rankings_parent.__class__.__name__,
        rankings.__class__.__name__,
        len(rankings) if isinstance(rankings, list) else None,
    )
    if not isinstance(rankings, list):
        logger.error(
            "Leaderboard rankings missing or invalid: ranking_key=%s parent=%s",
            ranking_key,
            _truncate_for_log(rankings_parent),
        )
        raise HTTPException(status_code=502, detail=f"Poe leaderboard GraphQL response missing {ranking_key}.topRankings")

    best_items_by_handle = {}
    skipped_rankings = []
    for index, ranking in enumerate(rankings):
        if not isinstance(ranking, dict):
            skipped_rankings.append({"index": index, "reason": "ranking is not a dict", "value": ranking})
            continue

        rank_metric = ranking.get("rankMetric")
        ranked = ranking.get("ranked")
        handle, handle_source = _extract_leaderboard_handle(ranked)
        has_valid_rank_metric = isinstance(rank_metric, (int, float)) and not isinstance(rank_metric, bool)
        if not has_valid_rank_metric or not handle:
            skipped_rankings.append(
                {
                    "index": index,
                    "reason": "missing required rankMetric/handle",
                    "rankMetric": rank_metric,
                    "handle_source": handle_source,
                    "ranking_keys": sorted(ranking.keys()),
                    "ranked_keys": sorted(ranked.keys()) if isinstance(ranked, dict) else None,
                    "ranking_preview": ranking,
                }
            )
            continue

        lowered_handle = handle.lower()
        candidate = {
            "handle": handle,
            "rankMetric": float(rank_metric),
            "_index": index,
        }
        existing = best_items_by_handle.get(lowered_handle)
        if existing is None or candidate["rankMetric"] > existing["rankMetric"]:
            best_items_by_handle[lowered_handle] = candidate
            logger.info(
                "Parsed leaderboard item: index=%s handle=%s rankMetric=%s source=%s",
                index,
                handle,
                rank_metric,
                handle_source,
            )
        else:
            logger.info(
                "Skipping duplicate leaderboard item: index=%s handle=%s rankMetric=%s source=%s",
                index,
                handle,
                rank_metric,
                handle_source,
            )

    parsed_items = sorted(
        best_items_by_handle.values(),
        key=lambda item: (-item["rankMetric"], item["_index"]),
    )
    items = [
        {
            "handle": item["handle"],
            "rank": rank,
            "rankMetric": item["rankMetric"],
        }
        for rank, item in enumerate(parsed_items[:count], start=1)
    ]
    logger.info(
        "Leaderboard parsing completed: requested=%s parsed=%s skipped=%s items=%s",
        count,
        len(items),
        len(skipped_rankings),
        _truncate_for_log(items),
    )
    if skipped_rankings:
        logger.warning("Skipped leaderboard rankings: %s", _truncate_for_log(skipped_rankings))
    if not items:
        logger.error(
            "Could not parse any leaderboard items from GraphQL response: ranking_key=%s response=%s",
            ranking_key,
            _truncate_for_log(data),
        )
        raise HTTPException(
            status_code=502,
            detail=f"Could not parse Poe leaderboard items from {ranking_key}.topRankings",
        )
    return items[:count]


async def fetch_single_rate(handle):
    async def fetch_page(client, current_handle):
        url = f"https://poe.com/{current_handle}"
        resp = await client.get(
            url,
            headers={"user-agent": HEADERS["user-agent"]},
            follow_redirects=False,
        )

        redirect_handle = extract_redirect_handle(resp.headers.get("location"))
        if redirect_handle and redirect_handle != current_handle:
            return {
                "status": "redirect",
                "handle": redirect_handle,
                "bot_id": None,
            }

        if resp.status_code == 404:
            return {
                "status": "not_found",
                "handle": current_handle,
                "bot_id": None,
            }

        bot_id = extract_bot_id(resp.text)
        return {
            "status": "ok" if bot_id else "missing_bot_id",
            "handle": current_handle,
            "bot_id": bot_id,
        }

    async def fetch_rates(client, current_handle, bot_id):
        payload = {
            "queryName": "RateCardModalQuery",
            "variables": {"botId": bot_id},
            "extensions": {"hash": "63afb70b30540bafd08f593b26c61f8bdd5b6818590742e5170f417709792788"},
        }
        resp = await client.post(POE_GRAPHQL_URL, headers=HEADERS, json=payload)
        data = resp.json()

        if data.get("errors"):
            return None

        pricing = data.get("data", {}).get("botById", {}).get("botPricing", {})
        markdown = pricing.get("rateMenuMarkdown", "")
        parsed = parse_rate_markdown(markdown)
        return {
            "handle": current_handle,
            "input": parsed["input"],
            "output": parsed["output"],
            "cache_discount": parsed["cache_discount"],
        }

    attempted = []
    pending = [handle]
    normalized_handle = normalize_handle_case(handle)
    if normalized_handle not in pending:
        pending.append(normalized_handle)

    async with httpx.AsyncClient(timeout=10.0) as page_client, httpx.AsyncClient(timeout=15.0) as gql_client:
        while pending:
            current_handle = pending.pop(0)
            if current_handle in attempted:
                continue
            attempted.append(current_handle)

            page_result = await fetch_page(page_client, current_handle)
            resolved_handle = page_result["handle"]

            if page_result["status"] in {"redirect", "not_found", "missing_bot_id"}:
                candidate = normalize_handle_case(resolved_handle)
                if candidate not in attempted and candidate not in pending:
                    pending.append(candidate)

                if page_result["status"] == "redirect" and resolved_handle not in attempted and resolved_handle not in pending:
                    pending.insert(0, resolved_handle)
                continue

            result = await fetch_rates(gql_client, resolved_handle, page_result["bot_id"])
            if result:
                return result

            candidate = normalize_handle_case(resolved_handle)
            if candidate not in attempted and candidate not in pending:
                pending.append(candidate)

    return None
