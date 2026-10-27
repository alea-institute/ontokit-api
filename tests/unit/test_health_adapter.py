"""Characterize cancellation in the installed SQLAlchemy asyncpg adapter."""

from __future__ import annotations

import asyncio
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import asyncpg
import pytest
from asyncpg.protocol.protocol import Protocol
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from ontokit.services.health import HealthService


class StalledDriver:
    """Synthetic network boundary; engine, pool, and dialect remain real."""

    def __init__(self, phase: str) -> None:
        self.phase = phase
        self.closed = False
        self.prepare_calls = 0
        self.fetchrow_calls = 0
        self.close_timeouts: list[float | None] = []
        self.terminate_calls = 0

    async def set_type_codec(self, *_args: object, **_kwargs: object) -> None:
        pass

    def transaction(self, *_args: object, **_kwargs: object) -> Mock:
        async def rollback() -> None:
            if self.phase == "rollback":
                await asyncio.Future()

        return Mock(start=AsyncMock(), rollback=AsyncMock(side_effect=rollback))

    async def prepare(self, *_args: object, **_kwargs: object) -> Mock:
        self.prepare_calls += 1
        if self.phase == "select":
            await asyncio.Future()
        return Mock(
            get_attributes=Mock(
                return_value=[SimpleNamespace(name="value", type=SimpleNamespace(oid=23))]
            ),
            fetch=AsyncMock(return_value=[(1,)]),
            get_statusmsg=Mock(return_value="SELECT 1"),
        )

    async def fetchrow(self, *_args: object, **_kwargs: object) -> None:
        self.fetchrow_calls += 1
        await asyncio.Future()

    def is_closed(self) -> bool:
        return self.closed

    async def close(self, *, timeout: float | None = None) -> None:
        self.close_timeouts.append(timeout)
        if not self.closed:
            async with asyncio.timeout(timeout):
                await asyncio.Future()

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.closed = True


@pytest.mark.parametrize("phase", ["select", "pooled_pre_ping", "rollback"])
async def test_adapter_cancellation_includes_shutdown_and_releases_resources(
    monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    driver = StalledDriver(phase)
    pre_ping = phase == "pooled_pre_ping"
    engine = create_async_engine(
        "postgresql+asyncpg://",
        async_creator=AsyncMock(return_value=driver),
        pool_pre_ping=pre_ping,
    )
    # Server introspection is outside the cancellation path being characterized.
    monkeypatch.setattr(engine.sync_engine.dialect, "initialize", lambda _connection: None)
    try:
        if pre_ping:
            initial = await engine.connect()
            await initial.close()
        service = HealthService(
            database_engine=engine,
            redis_factory=lambda: Mock(ping=AsyncMock(return_value=True), aclose=AsyncMock()),
        )
        tasks_before = asyncio.all_tasks()
        started = asyncio.get_running_loop().time()
        async with asyncio.timeout(5):
            checks = await service.check()
        elapsed = asyncio.get_running_loop().time() - started

        assert checks.database.status == ("unavailable" if phase == "rollback" else "timeout")
        assert checks.redis.status == "ok"
        assert not checks.ready
        # The adapter shields a two-second close after the operation (1 s) or
        # cleanup (250 ms) cancellation timer, including handle-less pre_ping.
        assert (2.0 if phase == "rollback" else 2.5) < elapsed < 5
        assert driver.close_timeouts == [2]
        assert driver.terminate_calls == 1
        assert driver.closed
        assert driver.prepare_calls == (0 if pre_ping else 1)
        assert driver.fetchrow_calls == (1 if pre_ping else 0)
        assert isinstance(engine.pool, AsyncAdaptedQueuePool)
        assert engine.pool.checkedout() == 0
        assert asyncio.all_tasks() <= tasks_before
    finally:
        await engine.dispose()


class SyntheticTransport:
    """Exercise real protocol shutdown without opening a socket."""

    def __init__(self, loop: asyncio.AbstractEventLoop, protocol: Protocol) -> None:
        self.loop = loop
        self.protocol = protocol
        self.close_started = asyncio.Event()
        self.aborts = 0

    def get_extra_info(self, _name: str) -> None:
        return None

    def write(self, data: bytes) -> None:
        if bytes(data).startswith(b"X"):
            self.close_started.set()

    def abort(self) -> None:
        self.aborts += 1
        self.loop.call_soon(self.protocol.connection_lost, None)


class ProtocolDriver(StalledDriver):
    """Real asyncpg connection lifecycle around the synthetic query boundary."""

    is_closed = asyncpg.Connection.is_closed
    close = asyncpg.Connection.close
    terminate = asyncpg.Connection.terminate
    _abort = asyncpg.Connection._abort

    def __init__(self, protocol: Protocol) -> None:
        super().__init__("select")
        self._protocol = protocol
        self._aborted = False

    def _cleanup(self) -> None:
        # This synthetic driver has no cached statements, listeners, or jobs.
        pass

    def _cancel_current_command(self, waiter: asyncio.Future[None]) -> None:
        # Avoid the real connection's separate cancellation socket.
        waiter.set_result(None)


def server_message(kind: bytes, payload: bytes) -> bytes:
    return kind + struct.pack("!I", len(payload) + 4) + payload


async def test_repeated_cancellation_can_outlive_probe_but_adapter_cleanup_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    connected = loop.create_future()
    parameters = SimpleNamespace(
        user="synthetic", password=None, database="synthetic", server_settings=None
    )
    protocol = Protocol(("synthetic", 5432), connected, parameters, asyncpg.Record, loop)
    transport = SyntheticTransport(loop, protocol)
    protocol.connection_made(transport)
    protocol.data_received(
        server_message(b"R", struct.pack("!I", 0))
        + server_message(b"S", b"server_version\x0017.0\x00")
        + server_message(b"Z", b"I")
    )
    await connected
    driver = ProtocolDriver(protocol)
    protocol.set_connection(driver)
    engine = create_async_engine(
        "postgresql+asyncpg://", async_creator=AsyncMock(return_value=driver)
    )
    monkeypatch.setattr(engine.sync_engine.dialect, "initialize", lambda _connection: None)
    service = HealthService(
        database_engine=engine,
        redis_factory=lambda: Mock(ping=AsyncMock(return_value=True), aclose=AsyncMock()),
    )
    tasks_before = asyncio.all_tasks()
    task = asyncio.create_task(service.check())
    pending: set[asyncio.Task[object]] = set()
    try:
        async with asyncio.timeout(5):
            await transport.close_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        pending = asyncio.all_tasks() - tasks_before
        assert pending
        assert transport.aborts == 0
        assert isinstance(engine.pool, AsyncAdaptedQueuePool)
        assert engine.pool.checkedout() == 0
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=3)
        assert transport.aborts == 1
        assert asyncio.all_tasks() <= tasks_before
    finally:
        if not task.done():
            task.cancel()
        for remaining in pending:
            if not remaining.done():
                remaining.cancel()
        await asyncio.gather(task, *pending, return_exceptions=True)
        await engine.dispose()
