from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import httpx
import html
import json
import logging
import re
import os
from datetime import datetime
from typing import List, Optional

import tomllib

app = FastAPI()
logger = logging.getLogger("poe_rate_dashboard")
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

CONFIG_FILE = "models_config.toml"
DATA_FILE = "static/data.json"
POE_GRAPHQL_URL = "https://poe.com/api/gql_POST"
CANONICAL_PREFIXES = {
    "gpt": "GPT",
    "claude": "Claude",
    "gemini": "Gemini",
}
LEADERBOARD_TYPE_TITLES = {
    "models": "Top Models",
    "apps": "Top Apps",
}
POE_LEADERBOARD_QUERY_NAME = "LeaderboardPageColumn_LeaderboardStatsQuery"
POE_LEADERBOARD_QUERY_HASH = "803d5007cefa51acd05611b9e5acec267561ec5f890e55f8e19589ccf7583511"
POE_LEADERBOARD_INTERVAL = "week"

update_status = {
    "running": False,
    "total": 0,
    "completed": 0,
    "current": "",
    "error": "",
    "updated_at": None,
}

HEADERS = {
    'accept': '*/*',
    'content-type': 'application/json',
    'origin': 'https://poe.com',
    'poe-formkey': '63c529af0ecc4d2491c4525e4b1fbf6b',
    'poe-queryname': 'RateCardModalQuery',
    'poe-revision': '3841a92fa633db990ddb39cc8bb28cb528659f45',
    'poegraphql': '1',
    'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36'
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

def normalize_handle_case(handle):
    if not handle:
        return handle

    parts = re.split(r'([\-_.])', handle)
    if not parts:
        return handle

    prefix = parts[0].lower()
    canonical = CANONICAL_PREFIXES.get(prefix)
    if not canonical:
        return handle

    parts[0] = canonical
    return "".join(parts)

def validate_leaderboard_type(value):
    leaderboard_type = (value or "models").strip().lower()
    if leaderboard_type not in LEADERBOARD_TYPE_TITLES:
        raise HTTPException(status_code=422, detail='type must be "models" or "apps"')
    return leaderboard_type

def load_config():
    with open(CONFIG_FILE, "rb") as f:
        return tomllib.load(f)

def save_config(cfg):
    handles = cfg.get("handles", [])
    serialized_handles = ", ".join(json.dumps(handle, ensure_ascii=False) for handle in handles)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        f.write(f"handles = [{serialized_handles}]\n")

def add_model_handle(cfg, handle):
    normalized_handle = normalize_handle_case((handle or "").strip())
    if not normalized_handle:
        raise HTTPException(status_code=422, detail="handle is required")

    existing_handles = {item.lower() for item in cfg["handles"]}
    if normalized_handle.lower() not in existing_handles:
        cfg["handles"].append(normalized_handle)
        return True
    return False

def extract_redirect_handle(location):
    if not location:
        return None

    match = re.search(r'https?://poe\.com/([^/?#]+)|^/([^/?#]+)', location, flags=re.IGNORECASE)
    if not match:
        return None

    handle = match.group(1) or match.group(2)
    return handle or None

def extract_bot_id(page_text):
    match = re.search(r'"botId":(\d+)', page_text)
    return int(match.group(1)) if match else None

def render_cache_discount_html(value):
    if not value:
        return "N/A"

    parts = []
    last_end = 0

    for match in re.finditer(r'\[([^\]]+)\]\((https?://[^\s)]+)\)', value):
        start, end = match.span()
        if start > last_end:
            parts.append(html.escape(value[last_end:start]))

        text, url = match.groups()
        safe_text = html.escape(text)
        safe_url = html.escape(url, quote=True)
        parts.append(f'<a href="{safe_url}" target="_blank" rel="noopener noreferrer">{safe_text}</a>')
        last_end = end

    if last_end == 0:
        return html.escape(value)

    if last_end < len(value):
        parts.append(html.escape(value[last_end:]))

    return "".join(parts)

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
        response_text = None
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

    items = []
    seen_handles = set()
    skipped_rankings = []
    for index, ranking in enumerate(rankings):
        if not isinstance(ranking, dict):
            skipped_rankings.append({"index": index, "reason": "ranking is not a dict", "value": ranking})
            continue

        rank = ranking.get("rank")
        ranked = ranking.get("ranked")
        handle, handle_source = _extract_leaderboard_handle(ranked)
        if not isinstance(rank, int) or not handle:
            skipped_rankings.append(
                {
                    "index": index,
                    "reason": "missing required rank/handle",
                    "rank": rank,
                    "handle_source": handle_source,
                    "ranking_keys": sorted(ranking.keys()),
                    "ranked_keys": sorted(ranked.keys()) if isinstance(ranked, dict) else None,
                    "ranking_preview": ranking,
                }
            )
            continue

        lowered_handle = handle.lower()
        if lowered_handle in seen_handles:
            logger.info(
                "Skipping duplicate leaderboard item: index=%s handle=%s rank=%s source=%s",
                index,
                handle,
                rank,
                handle_source,
            )
            continue

        items.append({
            "handle": handle,
            "rank": rank,
        })
        logger.info(
            "Parsed leaderboard item: index=%s handle=%s rank=%s source=%s",
            index,
            handle,
            rank,
            handle_source,
        )
        seen_handles.add(lowered_handle)

        if len(items) >= count:
            break

    items.sort(key=lambda item: item["rank"])
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
            headers={'user-agent': HEADERS['user-agent']},
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
            "extensions": {"hash": "63afb70b30540bafd08f593b26c61f8bdd5b6818590742e5170f417709792788"}
        }
        resp = await client.post('https://poe.com/api/gql_POST', headers=HEADERS, json=payload)
        data = resp.json()

        if data.get("errors"):
            return None

        pricing = data.get("data", {}).get("botById", {}).get("botPricing", {})
        markdown = pricing.get("rateMenuMarkdown", "")

        rates = {"input_usd": "N/A", "input_points": "N/A", "output_usd": "N/A", "output_points": "N/A", "cache_discount": "N/A"}

        # Poe may return Chinese or English labels based on request locale.
        ir = re.search(
            r'\|\s*(?:输入\s*[\(（]文本[\)）]|Input\s*\(text\)|输入|Input)\s*\|\s*(?P<price>.*?)\s*\|\s*(?P<points>.*?)\s*\|',
            markdown,
            flags=re.IGNORECASE
        )
        if ir:
            usd_bold = re.search(r'\*\*(\$[\d.]+)\*\*', ir.group('price'))
            usd_plain = re.search(r'(\$[\d.]+)', ir.group('price'))
            price = usd_bold.group(1) if usd_bold else (usd_plain.group(1) if usd_plain else None)
            rates["input_usd"] = f"{price}/百万词元" if price else "N/A"
            rates["input_points"] = ir.group('points').strip()

        or_row = re.search(
            r'\|\s*(?:输出\s*[\(（]文本[\)）]|Output\s*\(text\))\s*\|\s*(?P<price>.*?)\s*\|\s*(?P<points>.*?)\s*\|',
            markdown,
            flags=re.IGNORECASE
        )
        if or_row:
            usd_bold = re.search(r'\*\*(\$[\d.]+)\*\*', or_row.group('price'))
            usd_plain = re.search(r'(\$[\d.]+)', or_row.group('price'))
            price = usd_bold.group(1) if usd_bold else (usd_plain.group(1) if usd_plain else None)
            rates["output_usd"] = f"{price}/百万词元" if price else "N/A"
            rates["output_points"] = or_row.group('points').strip()

        cr = re.search(r'\|\s*(?:缓存折扣|Cache discount)\s*\|\s*(.*?)\s*\|', markdown, flags=re.IGNORECASE)
        if cr:
            rates["cache_discount"] = render_cache_discount_html(cr.group(1).strip())

        return {
            "handle": current_handle,
            "input": {"usd": rates["input_usd"], "points": rates["input_points"]},
            "output": {"usd": rates["output_usd"], "points": rates["output_points"]},
            "cache_discount": rates["cache_discount"]
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

# API Routes
@app.get("/api/config")
def get_config():
    return load_config()["handles"]

class ModelHandle(BaseModel):
    handle: str

class LeaderboardImportRequest(BaseModel):
    count: int = 30
    type: str = "models"

@app.post("/api/config")
def add_model(item: ModelHandle):
    cfg = load_config()
    if add_model_handle(cfg, item.handle):
        save_config(cfg)
    return cfg["handles"]

@app.delete("/api/config/{handle}")
def delete_model(handle: str):
    cfg = load_config()
    if handle in cfg["handles"]:
        cfg["handles"].remove(handle)
        save_config(cfg)
    return cfg["handles"]

@app.get("/api/poe/leaderboard")
async def get_poe_leaderboard(
    count: int = Query(default=30, ge=1, le=100),
    type: str = Query(default="models"),
):
    items = await fetch_poe_leaderboard_via_graphql(count, type)
    if not items:
        raise HTTPException(status_code=502, detail="Could not parse Poe leaderboard items")
    return items

@app.post("/api/config/import-leaderboard")
async def import_leaderboard_models(item: LeaderboardImportRequest):
    if item.count < 1 or item.count > 100:
        raise HTTPException(status_code=422, detail="count must be between 1 and 100")

    leaderboard_items = await fetch_poe_leaderboard_via_graphql(item.count, item.type)
    if not leaderboard_items:
        raise HTTPException(status_code=502, detail="Could not parse Poe leaderboard items")

    cfg = load_config()
    changed = False
    for leaderboard_item in leaderboard_items:
        changed = add_model_handle(cfg, leaderboard_item["handle"]) or changed

    if changed:
        save_config(cfg)

    return cfg["handles"]

@app.get("/api/update")
async def update_all(handles: Optional[List[str]] = Query(default=None)):
    cfg_handles = load_config()["handles"]

    if handles is None:
        targets = cfg_handles
    else:
        cfg_set = set(cfg_handles)
        targets = [h for h in handles if h in cfg_set]

    update_status["running"] = True
    update_status["total"] = len(targets)
    update_status["completed"] = 0
    update_status["current"] = ""
    update_status["error"] = ""
    update_status["updated_at"] = datetime.utcnow().isoformat()

    results = []
    try:
        for t in targets:
            update_status["current"] = t
            update_status["updated_at"] = datetime.utcnow().isoformat()
            res = await fetch_single_rate(t)
            if res:
                results.append(res)
            update_status["completed"] += 1
            update_status["updated_at"] = datetime.utcnow().isoformat()

        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=4, ensure_ascii=False)
        return results
    except Exception as exc:
        update_status["error"] = str(exc)
        raise
    finally:
        update_status["running"] = False
        update_status["current"] = ""
        update_status["updated_at"] = datetime.utcnow().isoformat()

@app.get("/api/update/status")
def get_update_status():
    return update_status

@app.get("/api/data")
def get_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

# Serve Web UI
app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
