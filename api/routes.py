import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query

from schemas import LeaderboardImportRequest, ModelHandle, UpdateTaskState


DATA_FILE = "static/data.json"


def add_model_handle(cfg, handle, normalize_handle_case):
    normalized_handle = normalize_handle_case((handle or "").strip())
    if not normalized_handle:
        raise HTTPException(status_code=422, detail="handle is required")

    existing_handles = {item.lower() for item in cfg["handles"]}
    if normalized_handle.lower() not in existing_handles:
        cfg["handles"].append(normalized_handle)
        return True
    return False


class UpdateStatusStore:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._tasks = {}
        self._active_task_id = None
        self._latest_task_id = None
        self._task_active_handles = {}
        self._task_failures = {}

    @staticmethod
    def _timestamp():
        return datetime.now(timezone.utc).isoformat()

    async def start_task(self, total):
        task_id = uuid.uuid4().hex
        task = UpdateTaskState(
            task_id=task_id,
            running=True,
            total=total,
            completed=0,
            current="",
            error="",
            updated_at=self._timestamp(),
        )
        async with self._lock:
            self._tasks[task_id] = task
            self._active_task_id = task_id
            self._latest_task_id = task_id
            self._task_active_handles[task_id] = set()
            self._task_failures[task_id] = {}
        return task_id

    @staticmethod
    def _format_active_handles(active_handles):
        if not active_handles:
            return ""

        handles = sorted(active_handles)
        preview = ", ".join(handles[:3])
        if len(handles) > 3:
            preview = f"{preview} (+{len(handles) - 3} running)"
        return preview

    @staticmethod
    def _format_failures(failures):
        if not failures:
            return ""
        return "; ".join(f"{handle}: {reason}" for handle, reason in sorted(failures.items()))

    async def set_current(self, task_id, current):
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            self._task_active_handles.setdefault(task_id, set()).add(current)
            task.current = self._format_active_handles(self._task_active_handles[task_id])
            task.updated_at = self._timestamp()

    async def mark_completed(self, task_id, current=None):
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            if current:
                self._task_active_handles.setdefault(task_id, set()).discard(current)
            task.completed += 1
            task.current = self._format_active_handles(self._task_active_handles.get(task_id, set()))
            task.updated_at = self._timestamp()

    async def mark_failed(self, task_id, current, error):
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            self._task_active_handles.setdefault(task_id, set()).discard(current)
            self._task_failures.setdefault(task_id, {})[current] = error
            task.completed += 1
            task.current = self._format_active_handles(self._task_active_handles.get(task_id, set()))
            task.error = self._format_failures(self._task_failures.get(task_id, {}))
            task.updated_at = self._timestamp()

    async def fail_task(self, task_id, error):
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            failures = self._task_failures.get(task_id, {})
            task.error = self._format_failures(failures) or error
            task.updated_at = self._timestamp()

    async def finish_task(self, task_id):
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.running = False
            task.current = ""
            task.updated_at = self._timestamp()
            if self._active_task_id == task_id:
                self._active_task_id = None
            self._latest_task_id = task_id
            self._task_active_handles.pop(task_id, None)

    async def is_active_task(self, task_id):
        async with self._lock:
            return self._active_task_id == task_id

    async def snapshot(self):
        async with self._lock:
            task_id = self._active_task_id or self._latest_task_id
            if task_id is None:
                return {
                    "running": False,
                    "total": 0,
                    "completed": 0,
                    "current": "",
                    "error": "",
                    "updated_at": None,
                }
            task = self._tasks[task_id]
            return {
                "running": task.running,
                "total": task.total,
                "completed": task.completed,
                "current": task.current,
                "error": task.error,
                "updated_at": task.updated_at,
            }


update_status_store = UpdateStatusStore()


def build_router(deps):
    router = APIRouter()

    @router.get("/api/config")
    def get_config():
        return deps.load_config()["handles"]

    @router.post("/api/config")
    def add_model(item: ModelHandle):
        cfg = deps.load_config()
        if add_model_handle(cfg, item.handle, deps.normalize_handle_case):
            deps.save_config(cfg)
        return cfg["handles"]

    @router.delete("/api/config/{handle}")
    def delete_model(handle: str):
        cfg = deps.load_config()
        if handle in cfg["handles"]:
            cfg["handles"].remove(handle)
            deps.save_config(cfg)
        return cfg["handles"]

    @router.get("/api/poe/leaderboard")
    async def get_poe_leaderboard(
        count: int = Query(default=30, ge=1, le=100),
        type: str = Query(default="models"),
    ):
        items = await deps.fetch_poe_leaderboard_via_graphql(count, type)
        if not items:
            raise HTTPException(status_code=502, detail="Could not parse Poe leaderboard items")
        return items

    @router.post("/api/config/import-leaderboard")
    async def import_leaderboard_models(item: LeaderboardImportRequest):
        if item.count < 1 or item.count > 100:
            raise HTTPException(status_code=422, detail="count must be between 1 and 100")

        leaderboard_items = await deps.fetch_poe_leaderboard_via_graphql(item.count, item.type)
        if not leaderboard_items:
            raise HTTPException(status_code=502, detail="Could not parse Poe leaderboard items")

        cfg = deps.load_config()
        changed = False
        for leaderboard_item in leaderboard_items:
            changed = add_model_handle(cfg, leaderboard_item["handle"], deps.normalize_handle_case) or changed

        if changed:
            deps.save_config(cfg)

        return cfg["handles"]

    @router.get("/api/update")
    async def update_all(handles: Optional[List[str]] = Query(default=None)):
        cfg_handles = deps.load_config()["handles"]

        if handles is None:
            targets = cfg_handles
        else:
            cfg_set = set(cfg_handles)
            targets = [h for h in handles if h in cfg_set]

        task_id = await update_status_store.start_task(len(targets))

        max_concurrency = max(1, int(getattr(deps, "UPDATE_MAX_CONCURRENCY", 5)))
        semaphore = asyncio.Semaphore(max_concurrency)

        async def fetch_target(target):
            async with semaphore:
                await update_status_store.set_current(task_id, target)
                try:
                    result = await deps.fetch_single_rate(target)
                except Exception as exc:
                    await update_status_store.mark_failed(task_id, target, str(exc))
                    return {"ok": False, "handle": target, "error": str(exc)}

                if result:
                    await update_status_store.mark_completed(task_id, target)
                    return {"ok": True, "handle": target, "result": result}

                error = "no rate data returned"
                await update_status_store.mark_failed(task_id, target, error)
                return {"ok": False, "handle": target, "error": error}

        try:
            outcomes = await asyncio.gather(*(fetch_target(target) for target in targets))
            results = [item["result"] for item in outcomes if item["ok"]]

            if await update_status_store.is_active_task(task_id):
                with open(deps.DATA_FILE, "w", encoding="utf-8") as f:
                    json.dump(results, f, indent=4, ensure_ascii=False)
            return results
        except Exception as exc:
            await update_status_store.fail_task(task_id, str(exc))
            raise
        finally:
            await update_status_store.finish_task(task_id)

    @router.get("/api/update/status")
    async def get_update_status():
        return await update_status_store.snapshot()

    @router.get("/api/data")
    def get_data():
        if os.path.exists(deps.DATA_FILE):
            with open(deps.DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        return []

    return router
