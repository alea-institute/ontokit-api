import asyncio
import logging
from collections.abc import Callable
from typing import Literal

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.pool import PoolProxiedConnection

from ontokit.core.config import settings
from ontokit.core.database import engine
from ontokit.schemas.health import DependencyChecks, HealthCheck

OPERATION_TIMEOUT = 1.0
CLEANUP_TIMEOUT = 0.25
logger = logging.getLogger(__name__)


def _redis_client() -> Redis:
    return Redis.from_url(
        str(settings.redis_url),
        socket_connect_timeout=OPERATION_TIMEOUT,
        socket_timeout=OPERATION_TIMEOUT,
        retry=Retry(NoBackoff(), 0),
        retry_on_timeout=False,
        retry_on_error=[],
    )


def _result(dependency: str, status: Literal["ok", "unavailable", "timeout"]) -> HealthCheck:
    messages = {
        "ok": f"{dependency} connection is available.",
        "unavailable": f"{dependency} connection is unavailable.",
        "timeout": f"{dependency} connection check timed out.",
    }
    return HealthCheck(status=status, message=messages[status])


class HealthService:
    def __init__(
        self,
        database_engine: AsyncEngine = engine,
        redis_factory: Callable[[], Redis] = _redis_client,
    ) -> None:
        self.database_engine = database_engine
        self.redis_factory = redis_factory

    async def check(self) -> DependencyChecks:
        """Collect both observations and await application-owned probe tasks."""
        async with asyncio.TaskGroup() as tasks:
            database = tasks.create_task(self._database())
            redis = tasks.create_task(self._redis())
        return DependencyChecks(database=database.result(), redis=redis.result())

    async def _database(self) -> HealthCheck:
        status: Literal["ok", "unavailable", "timeout"] = "ok"
        connection: AsyncConnection | None = None
        raw: PoolProxiedConnection | None = None
        # These timers request cancellation. SQLAlchemy/asyncpg may then shield
        # a two-second shutdown, including pre_ping before we receive a handle.
        # Library-owned cleanup can outlive a repeatedly cancelled probe.
        try:
            async with asyncio.timeout(OPERATION_TIMEOUT):
                connection = await self.database_engine.connect()
                raw = await connection.get_raw_connection()
                await connection.execute(text("SELECT 1"))
        except TimeoutError:
            status = "timeout"
        except Exception:
            status = "unavailable"
        finally:
            if connection is not None:
                try:
                    # AsyncConnection.__aexit__ shields a detached close task.
                    # Await close directly so our cleanup deadline owns its lifetime.
                    async with asyncio.timeout(CLEANUP_TIMEOUT):
                        await connection.close()
                except BaseException as exc:
                    if raw is not None and raw.is_valid:
                        # Outside SQLAlchemy's greenlet, asyncpg terminates directly;
                        # invalidation also returns the checkout to the existing pool.
                        try:
                            raw.invalidate()
                        except Exception:
                            logger.warning("Database readiness connection cleanup failed.")
                    if not isinstance(exc, Exception):
                        raise
                    status = "unavailable"
        return _result("Database", status)

    async def _redis(self) -> HealthCheck:
        status: Literal["ok", "unavailable", "timeout"] = "ok"
        client: Redis | None = None
        try:
            async with asyncio.timeout(OPERATION_TIMEOUT):
                client = self.redis_factory()
                if not await client.ping():
                    status = "unavailable"
        except (TimeoutError, RedisTimeoutError):
            status = "timeout"
        except Exception:
            status = "unavailable"
        finally:
            if client is not None:
                try:
                    async with asyncio.timeout(CLEANUP_TIMEOUT):
                        await client.aclose(close_connection_pool=True)
                except Exception:
                    status = "unavailable"
        return _result("Redis", status)


def get_health_service() -> HealthService:
    return HealthService()
