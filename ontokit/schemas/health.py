"""Public dependency connectivity results, separate from process liveness."""

from typing import Literal

from pydantic import BaseModel


class HealthCheck(BaseModel):
    """One bounded connectivity observation with a fixed, public-safe message."""

    status: Literal["ok", "unavailable", "timeout"]
    message: str


class DependencyChecks(BaseModel):
    database: HealthCheck
    redis: HealthCheck

    @property
    def ready(self) -> bool:
        return self.database.status == "ok" and self.redis.status == "ok"


class ReadinessResponse(BaseModel):
    """Connectivity only; does not certify workers, providers, indexes, or storage."""

    scope: Literal["dependencies"] = "dependencies"
    status: Literal["ready", "not_ready"]
    request_id: str
    checks: DependencyChecks
