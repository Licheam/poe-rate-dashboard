import logging
import os
import sys
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.routes import DATA_FILE, build_router, update_status_store
from repositories.config_repo import CONFIG_FILE, load_config, save_config
from schemas import LeaderboardImportRequest, ModelHandle
from services import poe_client
from services.poe_client import (
    HEADERS,
    LEADERBOARD_TYPE_TITLES,
    POE_GRAPHQL_URL,
    POE_LEADERBOARD_INTERVAL,
    POE_LEADERBOARD_QUERY_HASH,
    POE_LEADERBOARD_QUERY_NAME,
    validate_leaderboard_type,
)
from services.rate_parser import (
    CANONICAL_PREFIXES,
    extract_bot_id,
    extract_redirect_handle,
    normalize_handle_case,
    parse_rate_markdown,
    render_cache_discount_html,
)


logger = logging.getLogger("poe_rate_dashboard")
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

UPDATE_MAX_CONCURRENCY = max(1, int(os.getenv("UPDATE_MAX_CONCURRENCY", "5")))


@asynccontextmanager
async def lifespan(app: FastAPI):
    client = httpx.AsyncClient(follow_redirects=True)
    app.state.http_client = client
    poe_client.set_async_client(client)
    try:
        yield
    finally:
        poe_client.set_async_client(None)
        await client.aclose()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(build_router(sys.modules[__name__]))
app.mount("/", StaticFiles(directory="static", html=True), name="static")


async def fetch_poe_leaderboard_via_graphql(count: int, type: str = "models"):
    return await poe_client.fetch_poe_leaderboard_via_graphql(count, type)


async def fetch_single_rate(handle):
    return await poe_client.fetch_single_rate(handle)


async def get_update_status():
    return await update_status_store.snapshot()


update_status = update_status_store
get_config = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/api/config" and "GET" in getattr(route, "methods", set()))
add_model = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/api/config" and "POST" in getattr(route, "methods", set()))
delete_model = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/api/config/{handle}" and "DELETE" in getattr(route, "methods", set()))
get_poe_leaderboard = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/api/poe/leaderboard")
import_leaderboard_models = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/api/config/import-leaderboard")
update_all = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/api/update")
get_data = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/api/data")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
