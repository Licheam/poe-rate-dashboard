from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import httpx
import json
import toml
import re
import os
from datetime import datetime
from typing import List, Optional

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

CONFIG_FILE = "models_config.toml"
DATA_FILE = "static/data.json"
CANONICAL_PREFIXES = {
    "gpt": "GPT",
    "claude": "Claude",
    "gemini": "Gemini",
}

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
            rates["cache_discount"] = cr.group(1).strip()

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
    with open(CONFIG_FILE, "r") as f:
        return toml.load(f)["handles"]

class ModelHandle(BaseModel):
    handle: str

@app.post("/api/config")
def add_model(item: ModelHandle):
    with open(CONFIG_FILE, "r") as f:
        cfg = toml.load(f)

    normalized_handle = normalize_handle_case(item.handle)
    existing_handles = {handle.lower() for handle in cfg["handles"]}
    if normalized_handle.lower() not in existing_handles:
        cfg["handles"].append(normalized_handle)
        with open(CONFIG_FILE, "w") as f:
            toml.dump(cfg, f)
    return cfg["handles"]

@app.delete("/api/config/{handle}")
def delete_model(handle: str):
    with open(CONFIG_FILE, "r") as f:
        cfg = toml.load(f)
    if handle in cfg["handles"]:
        cfg["handles"].remove(handle)
        with open(CONFIG_FILE, "w") as f:
            toml.dump(cfg, f)
    return cfg["handles"]

@app.get("/api/update")
async def update_all(handles: Optional[List[str]] = Query(default=None)):
    with open(CONFIG_FILE, "r") as f:
        cfg_handles = toml.load(f)["handles"]

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
