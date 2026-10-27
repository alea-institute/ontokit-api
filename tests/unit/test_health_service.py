"""Dependency readiness owns its resources and reports only safe connectivity facts."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from redis.asyncio import Redis
from redis.asyncio.connection import Connection, ConnectionPool
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from ontokit.services.health import HealthService, get_health_service


class ProbeResource:
    """Cooperative driver boundary with observable operation and cleanup lifetimes."""

    def __init__(self, *, failure: str | None = None) -> None:
        self.failure = failure
        self.operation_started = asyncio.Event()
        self.cleanup_started = asyncio.Event()
        self.released = False
        self.cleanup_calls = 0
        self.operation_calls = 0
        self.active_operations = 0

    async def operation(self) -> bool:
        self.operation_calls += 1
        self.operation_started.set()
        self.active_operations += 1
        try:
            if self.failure == "operation_stall":
                await asyncio.Future()
            if self.failure == "operation_error":
                raise RuntimeError("postgresql://demo:synthetic-secret@private:5432/data SELECT 1")
            if self.failure == "socket_timeout":
                raise RedisTimeoutError("synthetic-secret")
            return self.failure != "negative_ping"
        finally:
            self.active_operations -= 1

    async def cleanup(self, *, close_connection_pool: bool | None = None) -> None:
        if close_connection_pool is not None:
            assert close_connection_pool
        self.cleanup_calls += 1
        self.cleanup_started.set()
        try:
            if self.failure == "cleanup_stall":
                await asyncio.Future()
            if self.failure == "cleanup_error":
                raise RuntimeError("synthetic-secret cleanup")
        finally:
            self.released = True


def resources(
    *, db_failure: str | None = None, redis_failure: str | None = None
) -> tuple[HealthService, ProbeResource, ProbeResource, Mock, Mock]:
    database = ProbeResource(failure=db_failure)
    redis = ProbeResource(failure=redis_failure)
    raw = Mock()
    raw.is_valid = True

    def invalidate() -> None:
        raw.is_valid = False
        database.released = True

    raw.invalidate.side_effect = invalidate
    connection = Mock()
    connection.get_raw_connection = AsyncMock(return_value=raw)

    async def execute(_query: object) -> None:
        await database.operation()

    connection.execute = AsyncMock(side_effect=execute)
    connection.close = AsyncMock(side_effect=database.cleanup)
    engine = Mock()
    engine.connect = AsyncMock(return_value=connection)
    if db_failure == "connect_stall":

        async def connect() -> None:
            database.operation_started.set()
            await asyncio.Future()

        engine.connect.side_effect = connect
    elif db_failure == "connect_error":
        engine.connect.side_effect = RuntimeError("synthetic-secret connect")
    client = Mock()
    client.ping = AsyncMock(side_effect=redis.operation)
    client.aclose = AsyncMock(side_effect=redis.cleanup)
    factory = Mock(return_value=client)
    return (
        HealthService(database_engine=engine, redis_factory=factory),
        database,
        redis,
        connection,
        raw,
    )


async def test_readiness_uses_only_select_one_and_ping_and_releases_resources() -> None:
    service, database, redis, connection, raw = resources()
    checks = await service.check()
    assert checks.ready
    assert checks.model_dump() == {
        "database": {"status": "ok", "message": "Database connection is available."},
        "redis": {"status": "ok", "message": "Redis connection is available."},
    }
    assert str(connection.execute.call_args.args[0]) == "SELECT 1"
    assert database.operation_calls == redis.operation_calls == 1
    assert database.cleanup_calls == redis.cleanup_calls == 1
    assert database.released and redis.released
    raw.invalidate.assert_not_called()


@pytest.mark.parametrize(
    ("db_failure", "redis_failure", "expected_database", "expected_redis"),
    [
        ("connect_error", None, "unavailable", "ok"),
        ("operation_error", None, "unavailable", "ok"),
        (None, "operation_error", "ok", "unavailable"),
        ("operation_error", "operation_error", "unavailable", "unavailable"),
        (None, "socket_timeout", "ok", "timeout"),
        (None, "negative_ping", "ok", "unavailable"),
    ],
)
async def test_failures_remain_independent_and_public_safe(
    db_failure: str | None,
    redis_failure: str | None,
    expected_database: str,
    expected_redis: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, database, redis, _, _ = resources(db_failure=db_failure, redis_failure=redis_failure)
    checks = await service.check()
    assert not checks.ready
    assert checks.database.status == expected_database
    assert checks.redis.status == expected_redis
    for sensitive in ("synthetic-secret", "postgresql://", "SELECT 1", "private:5432"):
        assert sensitive not in checks.model_dump_json()
        assert sensitive not in caplog.text
    assert redis.released
    if db_failure != "connect_error":
        assert database.released


@pytest.mark.parametrize("db_failure", ["connect_stall", "operation_stall"])
async def test_concurrent_stalls_finish_within_two_seconds_and_leave_no_tasks(
    db_failure: str,
) -> None:
    service, database, redis, connection, _ = resources(
        db_failure=db_failure, redis_failure="operation_stall"
    )
    tasks_before = asyncio.all_tasks()
    started = asyncio.get_running_loop().time()
    checks = await service.check()
    assert asyncio.get_running_loop().time() - started < 2
    assert checks.database.status == checks.redis.status == "timeout"
    assert not checks.ready
    assert redis.released and redis.active_operations == 0
    if db_failure == "connect_stall":
        connection.close.assert_not_awaited()
    else:
        assert database.released and database.active_operations == 0
    assert asyncio.all_tasks() <= tasks_before


@pytest.mark.parametrize("failure", ["cleanup_stall", "cleanup_error"])
async def test_cleanup_failure_is_unavailable_and_database_connection_is_invalidated(
    failure: str,
) -> None:
    service, database, redis, _, raw = resources(db_failure=failure, redis_failure=failure)
    tasks_before = asyncio.all_tasks()
    started = asyncio.get_running_loop().time()
    checks = await service.check()
    assert asyncio.get_running_loop().time() - started < 2
    assert not checks.ready
    assert checks.database.status == checks.redis.status == "unavailable"
    assert database.released and redis.released
    raw.invalidate.assert_called_once_with()
    assert asyncio.all_tasks() <= tasks_before


@pytest.mark.parametrize("phase", ["operation", "cleanup"])
async def test_request_cancellation_propagates_after_cleanup_without_detached_tasks(
    phase: str,
) -> None:
    failure = f"{phase}_stall"
    service, database, redis, _, raw = resources(db_failure=failure, redis_failure=failure)
    tasks_before = asyncio.all_tasks()
    task = asyncio.create_task(service.check())
    async with asyncio.timeout(2):
        await getattr(database, f"{phase}_started").wait()
        await getattr(redis, f"{phase}_started").wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert database.released and redis.released
    assert database.active_operations == redis.active_operations == 0
    if phase == "cleanup":
        raw.invalidate.assert_called_once_with()
    assert asyncio.all_tasks() <= tasks_before


async def test_redis_is_reprobed_after_failure_without_shared_pool_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ontokit.api.utils.redis as arq_module
    import ontokit.main as main_module

    service, _, _, _, _ = resources()
    clients = [ProbeResource(failure="operation_error"), ProbeResource()]
    factory = Mock()
    factory.side_effect = [
        Mock(
            ping=AsyncMock(side_effect=client.operation),
            aclose=AsyncMock(side_effect=client.cleanup),
        )
        for client in clients
    ]
    service.redis_factory = factory
    monkeypatch.setattr(main_module, "redis_pool", None)
    original_arq_pool = arq_module._arq_pool
    first = await service.check()
    second = await service.check()
    assert first.redis.status == "unavailable"
    assert second.ready
    assert factory.call_count == 2
    assert all(client.released for client in clients)
    assert main_module.redis_pool is None
    assert arq_module._arq_pool is original_arq_pool


async def test_default_redis_factory_bounds_socket_time_and_disables_retries() -> None:
    service = get_health_service()
    client = service.redis_factory()
    options = client.connection_pool.connection_kwargs
    assert options["socket_connect_timeout"] == 1.0
    assert options["socket_timeout"] == 1.0
    assert options["retry_on_timeout"] is False
    attempt = AsyncMock(side_effect=RedisConnectionError("Unavailable"))
    with pytest.raises(RedisConnectionError):
        await options["retry"].call_with_retry(attempt, AsyncMock())
    attempt.assert_awaited_once()


async def test_database_invalidation_failure_is_safe_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, _, _, _, raw = resources(db_failure="cleanup_error")
    raw.invalidate.side_effect = RuntimeError("synthetic-secret from driver cleanup")
    checks = await service.check()
    assert checks.database.status == "unavailable"
    assert checks.redis.status == "ok"
    assert "synthetic-secret" not in caplog.text
    assert "synthetic-secret" not in checks.model_dump_json()
    assert "cleanup" in caplog.text


@pytest.mark.parametrize("cancel_request", [False, True])
async def test_real_redis_disconnect_closes_stalled_writer_without_detached_tasks(
    cancel_request: bool,
) -> None:
    service, _, _, _, _ = resources()
    connection = Connection(socket_connect_timeout=1.0, socket_timeout=1.0)
    closing = asyncio.Event()

    async def wait_closed() -> None:
        closing.set()
        await asyncio.Future()

    writer = Mock(wait_closed=AsyncMock(side_effect=wait_closed))
    connection._writer = writer
    connection._reader = Mock()
    pool = ConnectionPool()
    pool._available_connections.append(connection)
    client = Redis(connection_pool=pool)
    client.ping = AsyncMock(return_value=True)
    service.redis_factory = Mock(return_value=client)
    tasks_before = asyncio.all_tasks()
    task = asyncio.create_task(service.check())
    async with asyncio.timeout(2):
        await closing.wait()
    if cancel_request:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        checks = await task
        assert checks.redis.status == "unavailable"
    writer.close.assert_called_once()
    assert connection._writer is None
    assert connection._reader is None
    assert asyncio.all_tasks() <= tasks_before
