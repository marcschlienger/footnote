from typing import Optional
from pydantic import BaseModel
from enum import Enum


class JobStatusEnum(str, Enum):
    PENDING = "pending"
    SEARCHING = "searching"
    CRAWLING = "crawling"
    SAVING = "saving"
    COMPLETED = "completed"
    FAILED = "failed"


class PushKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscription(BaseModel):
    endpoint: str
    keys: PushKeys


class ResearchRequest(BaseModel):
    question: str
    subscription: Optional[PushSubscription] = None


class ResearchResponse(BaseModel):
    status: str
    message: str
    job_id: str


class JobStatus(BaseModel):
    status: JobStatusEnum
    question: str
    progress: Optional[str] = None
    result: Optional[str] = None
    error: Optional[str] = None
