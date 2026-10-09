"""Offline wire proof: production routes, JWT verification, PostgreSQL and Redis.

Run against disposable services with B12_TEST_DATABASE_URL and
B12_TEST_REDIS_URL (Redis may use unix:///path). Uvicorn and clients use a
temporary Unix socket. Only JWKS fetching is replaced with an in-memory test key;
the API lifecycle is isolated from unrelated external startup integrations.
"""

import asyncio
import base64
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import partial
from uuid import uuid4

import httpx
import jwt
import pytest
import redis.asyncio as aioredis
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from sqlalchemy import delete, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from websockets.asyncio.client import unix_connect
from websockets.exceptions import ConnectionClosed

from ontokit.api.routes import lint, projects, quality
from ontokit.api.utils import ws_auth, ws_forward
from ontokit.core import auth
from ontokit.core.constants import ONTOLOGY_INDEX_UPDATES_CHANNEL
from ontokit.core.database import Base, get_db
from ontokit.core.logging_filters import install_token_redaction
from ontokit.models.lint import LintRun
from ontokit.models.ontology_index import OntologyIndexStatus
from ontokit.models.project import Project, ProjectMember

pytestmark = pytest.mark.skipif(
    not (os.environ.get("B12_TEST_DATABASE_URL") and os.environ.get("B12_TEST_REDIS_URL")),
    reason="requires disposable B12 PostgreSQL and Redis services",
)

CASES = [
    ("ontology/index-ws", ONTOLOGY_INDEX_UPDATES_CHANNEL, "index_complete"),
    ("lint/ws", lint.LINT_UPDATES_CHANNEL, "lint_complete"),
    ("quality/ws", quality.QUALITY_UPDATES_CHANNEL, "duplicates_complete"),
]


@pytest.fixture
async def harness(tmp_path, monkeypatch, request):
    engine = create_async_engine(os.environ["B12_TEST_DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    redis = aioredis.from_url(os.environ["B12_TEST_REDIS_URL"])
    project_id, other_id = uuid4(), uuid4()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = "b12-offline-test"

    async def jwks(**_kwargs):
        return {"keys": [jwk]}

    monkeypatch.setattr(auth, "get_jwks", jwks)
    monkeypatch.setattr(auth.settings, "auth_mode", "required")
    monkeypatch.setattr(auth.settings, "zitadel_issuer", "https://b12.invalid")
    monkeypatch.setattr(auth.settings, "zitadel_client_id", "b12-test")
    monkeypatch.setattr(auth.settings, "git_repos_base_path", str(tmp_path / "repos"))
    monkeypatch.setattr(ws_auth, "async_session_maker", sessions)
    monkeypatch.setattr(ws_forward, "async_session_maker", sessions)
    monkeypatch.setattr(
        ws_forward,
        "forward_project_events",
        partial(ws_forward.forward_project_events, reauthorize_interval=0.1),
    )

    async def pool():
        return redis

    for route in (lint, projects, quality):
        monkeypatch.setattr(route, "get_arq_pool", pool)
    monkeypatch.setattr(quality, "_get_redis", lambda: redis)

    async def db():
        async with sessions() as session:
            yield session

    app = FastAPI()
    for route in (projects, lint, quality):
        app.include_router(route.router, prefix="/api/v1/projects")
    app.dependency_overrides[get_db] = db
    install_token_redaction()

    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add_all(
            [
                Project(id=project_id, name="B12 private", owner_id="owner", is_public=False),
                Project(id=other_id, name="B12 other", owner_id="owner", is_public=False),
                ProjectMember(project_id=project_id, user_id="member", role="editor"),
                ProjectMember(project_id=project_id, user_id="owner", role="owner"),
                ProjectMember(project_id=other_id, user_id="owner", role="owner"),
                OntologyIndexStatus(project_id=project_id, branch="main", status="indexing"),
                LintRun(project_id=project_id, status="running"),
            ]
        )
        await session.commit()

    def token(subject="owner", expires=None):
        now = int(time.time())
        return jwt.encode(
            {
                "sub": subject,
                "iss": "https://b12.invalid",
                "aud": "b12-test",
                "iat": now,
                "exp": expires or now + 300,
                "name": "Synthetic test user",
                "email": "test@b12.invalid",
            },
            key,
            algorithm="RS256",
            headers={"kid": jwk["kid"]},
        )

    socket_path = str(tmp_path / "api.sock")

    @asynccontextmanager
    async def server():
        instance = uvicorn.Server(
            uvicorn.Config(
                app,
                uds=socket_path,
                lifespan="off",
                log_config=None,
                ws=request.param,
                timeout_graceful_shutdown=1,
            )
        )
        task = asyncio.create_task(instance.serve())
        try:
            async with asyncio.timeout(5):
                while not instance.started:
                    if task.done():
                        task.result()
                    await asyncio.sleep(0.01)
            yield instance
        finally:
            instance.should_exit = True
            await asyncio.wait_for(task, 5)

    def connect(path, credential=None, query=False, query_name="token"):
        url = "ws://localhost/api/v1/projects/" + path
        protocols = None
        if credential is not None:
            if query:
                url += "?" + query_name + "=" + credential
            else:
                encoded = base64.urlsafe_b64encode(credential.encode()).decode().rstrip("=")
                protocols = ["ontokit.bearer.v1", "ontokit.token." + encoded]
        return unix_connect(socket_path, uri=url, subprotocols=protocols, open_timeout=3)

    async def ready(channel, count):
        observed = None
        try:
            async with asyncio.timeout(3):
                while True:
                    observed = await redis.pubsub_numsub(channel)
                    if observed[0][1] == count:
                        return
                    await asyncio.sleep(0.01)
        except TimeoutError:
            pytest.fail(f"Expected {count} subscriptions on {channel}, observed {observed}")

    try:
        yield project_id, other_id, sessions, redis, token, server, connect, ready, socket_path
    finally:
        await redis.aclose()
        await engine.dispose()


async def assert_closed(socket, code):
    with pytest.raises(ConnectionClosed) as error:
        await asyncio.wait_for(socket.recv(), 3)
    assert error.value.rcvd.code == code


@pytest.mark.parametrize("endpoint,channel,event_type", CASES)
@pytest.mark.parametrize("harness", ["websockets", "websockets-sansio"], indirect=True)
async def test_auth_fanout_restart_recovery_and_revocation(
    harness,
    caplog,
    endpoint,
    channel,
    event_type,
):
    pid, other, sessions, redis, token, server, connect, ready, socket_path = harness
    owner_token, member_token = token(), token("member")
    path = f"{pid}/{endpoint}"
    event = {"project_id": str(pid), "type": event_type}
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.DEBUG, logger="uvicorn.error")
    async with server():
        for credential, project, code in [
            (None, pid, 4001),
            ("garbage", pid, 4001),
            (token("unrelated"), pid, 4003),
            (owner_token, uuid4(), 4004),
            (token(expires=int(time.time()) - 1), pid, 4001),
        ]:
            async with connect(f"{project}/{endpoint}", credential) as ws:
                await assert_closed(ws, code)
        for query_name in ("token", "to%6ben"):
            async with connect(path, owner_token, query=True, query_name=query_name) as legacy:
                await ready(channel, 1)
                await redis.publish(channel, json.dumps(event))
                assert json.loads(await asyncio.wait_for(legacy.recv(), 3)) == event
            await ready(channel, 0)

        async with (
            connect(path, owner_token) as first,
            connect(path, member_token) as second,
            connect(f"{other}/{endpoint}", owner_token) as isolated,
        ):
            assert first.subprotocol == "ontokit.bearer.v1"
            await ready(channel, 3)
            await redis.publish(channel, json.dumps(event))
            for ws in (first, second):
                assert json.loads(await asyncio.wait_for(ws.recv(), 3)) == event
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(isolated.recv(), 0.2)
        await ready(channel, 0)

        restarting = await connect(path, owner_token)
        await ready(channel, 1)
    await assert_closed(restarting, 1012)

    # Workers persist results while the API is down; pub/sub deliberately has no replay.
    now = datetime.now(UTC)
    async with sessions() as session:
        await session.execute(
            update(OntologyIndexStatus)
            .where(
                OntologyIndexStatus.project_id == pid,
            )
            .values(status="ready", entity_count=42, indexed_at=now)
        )
        await session.execute(
            update(LintRun)
            .where(
                LintRun.project_id == pid,
            )
            .values(status="completed", issues_found=0, completed_at=now)
        )
        await session.commit()
    result = {"clusters": [], "threshold": 0.85, "checked_at": now.isoformat()}
    await redis.set(f"duplicates_job:{pid}:b12-test", json.dumps(result))
    assert await redis.publish(channel, json.dumps(event)) == 0

    async with server():
        async with connect(path, owner_token) as recovered:
            await ready(channel, 1)
            async with httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(uds=socket_path),
                base_url="http://localhost",
                headers={"Authorization": "Bearer " + owner_token},
            ) as http:
                for route, expected in [
                    ("ontology/index-status?branch=main", "ready"),
                    ("lint/status", "completed"),
                    ("quality/duplicates/jobs/b12-test", None),
                ]:
                    response = await http.get(f"/api/v1/projects/{pid}/{route}")
                    assert response.status_code == 200, response.text
                    payload = response.json()
                    if route.startswith("ontology"):
                        assert payload["status"] == expected
                        assert payload["entity_count"] == 42
                    elif route.startswith("lint"):
                        assert payload["last_run"]["status"] == expected
                    else:
                        assert payload == result
            await redis.publish(channel, json.dumps(event))
            assert json.loads(await asyncio.wait_for(recovered.recv(), 3)) == event
        await ready(channel, 0)
        async with connect(path, member_token) as revoked:
            await ready(channel, 1)
            async with sessions() as session:
                await session.execute(
                    delete(ProjectMember).where(
                        ProjectMember.project_id == pid,
                        ProjectMember.user_id == "member",
                    )
                )
                await session.commit()
            await assert_closed(revoked, 4003)
        await ready(channel, 0)
        async with connect(path, token(expires=int(time.time()) + 2)) as expired:
            await ready(channel, 1)
            await assert_closed(expired, 4001)
    assert owner_token not in caplog.text
    assert member_token not in caplog.text
    for credential in (owner_token, member_token):
        assert base64.urlsafe_b64encode(credential.encode()).decode().rstrip("=") not in caplog.text
    assert "token=REDACTED" in caplog.text
