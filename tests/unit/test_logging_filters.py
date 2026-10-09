"""Redaction preserves uvicorn's real access and handshake record formats."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from uvicorn import Config
from uvicorn.logging import AccessFormatter
from uvicorn.protocols.http.h11_impl import RequestResponseCycle
from uvicorn.protocols.websockets.websockets_sansio_impl import WebSocketsSansIOProtocol
from uvicorn.server import ServerState

from ontokit.core.logging_filters import TokenQueryRedactionFilter, install_token_redaction


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/ws?token=secret&other=kept", "/ws?token=REDACTED&other=kept"),
        ("/ws?other=kept&token=one&token=two", "/ws?other=kept&token=REDACTED&token=REDACTED"),
        ('WebSocket /ws?token=secret" [accepted]', 'WebSocket /ws?token=REDACTED" [accepted]'),
        ("/ws?token=&other=kept", "/ws?token=REDACTED&other=kept"),
        ("/ws?other=kept", "/ws?other=kept"),
    ],
)
def test_query_redaction(value, expected) -> None:
    record = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, value, (), None)
    assert TokenQueryRedactionFilter().filter(record)
    assert record.getMessage() == expected


def test_mapping_arguments_and_exception_text() -> None:
    try:
        raise RuntimeError("Failed /ws?token=secret&other=kept")
    except RuntimeError:
        import sys

        record = logging.LogRecord(
            "uvicorn.error",
            logging.ERROR,
            __file__,
            1,
            "Failed %(path)s",
            ({"path": "/ws?token=secret&other=kept"},),
            sys.exc_info(),
        )
    TokenQueryRedactionFilter().filter(record)
    rendered = logging.Formatter().format(record)
    assert "secret" not in rendered
    assert "RuntimeError: Failed /ws?token=REDACTED&other=kept" in rendered


@pytest.mark.asyncio
async def test_real_uvicorn_http_access_record(caplog) -> None:
    logger = logging.getLogger("uvicorn.access")
    # Exercise uvicorn's actual record emission rather than copying its template.
    cycle = RequestResponseCycle(
        scope={
            "type": "http",
            "method": "GET",
            "path": "/ws",
            "query_string": b"token=secret&other=kept",
            "http_version": "1.1",
            "headers": [],
            "client": ("127.0.0.1", 1234),
        },
        conn=Mock(),
        transport=Mock(),
        flow=SimpleNamespace(write_paused=False),
        logger=logging.getLogger("uvicorn.error"),
        access_logger=logger,
        access_log=True,
        default_headers=[],
        message_event=asyncio.Event(),
        on_response=Mock(),
    )
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        await cycle.send({"type": "http.response.start", "status": 200, "headers": []})
    record = next(r for r in caplog.records if r.name == "uvicorn.access")
    TokenQueryRedactionFilter().filter(record)
    assert len(record.args) == 5
    assert record.args[-1] == 200
    rendered = AccessFormatter(use_colors=False).format(record)
    assert "secret" not in rendered
    assert "/ws?token=REDACTED&other=kept" in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [{"type": "websocket.accept"}, {"type": "websocket.close"}])
async def test_real_uvicorn_websocket_handshake_record(caplog, message) -> None:
    async def app(scope, receive, send):
        pass

    protocol = WebSocketsSansIOProtocol(Config(app, log_config=None), ServerState(), {})
    protocol.scope = {
        "type": "websocket",
        "path": "/ws",
        "query_string": b"token=secret&other=kept",
        "client": ("127.0.0.1", 1234),
    }
    protocol.conn = Mock()
    protocol.conn.data_to_send.return_value = [b"response"]
    protocol.transport = Mock()
    protocol.transport.is_closing.return_value = True
    protocol.response = SimpleNamespace(headers={"Date": "unused"})
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        await protocol.send(message)
    record = next(r for r in caplog.records if "WebSocket" in r.getMessage())
    assert record.name == "uvicorn.error"
    TokenQueryRedactionFilter().filter(record)
    assert "secret" not in record.getMessage()
    assert "/ws?token=REDACTED&other=kept" in record.getMessage()


def test_installation_is_idempotent_and_main_installs_filters() -> None:
    import ontokit.main  # noqa: F401

    for name in ("uvicorn.access", "uvicorn.error"):
        assert any(
            isinstance(f, TokenQueryRedactionFilter) for f in logging.getLogger(name).filters
        )
    install_token_redaction()
    install_token_redaction()
    for name in ("uvicorn.access", "uvicorn.error"):
        assert (
            sum(isinstance(f, TokenQueryRedactionFilter) for f in logging.getLogger(name).filters)
            == 1
        )
