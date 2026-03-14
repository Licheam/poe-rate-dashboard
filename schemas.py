from typing import Optional

from pydantic import BaseModel


class ModelHandle(BaseModel):
    handle: str


class LeaderboardImportRequest(BaseModel):
    count: int = 30
    type: str = "models"


class UpdateTaskState(BaseModel):
    task_id: str
    running: bool = False
    total: int = 0
    completed: int = 0
    current: str = ""
    error: str = ""
    updated_at: Optional[str] = None
