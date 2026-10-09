"""Redact legacy query credentials from uvicorn request and exception logs."""

import logging
import re
import traceback

_TOKEN_QUERY = re.compile(r"([?&]token=)[^&\s\"'<>#]*", re.IGNORECASE)


def _redact(value: str) -> str:
    return _TOKEN_QUERY.sub(r"\1REDACTED", value)


class TokenQueryRedactionFilter(logging.Filter):
    """Keep record arguments intact for uvicorn's five-field AccessFormatter.

    HTTP paths are arguments on uvicorn.access; WebSocket handshake paths are
    arguments on uvicorn.error, including accepted and denied handshakes. Also
    redact preformatted messages and cached traceback text on those loggers.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                _redact(arg) if isinstance(arg, str) else arg for arg in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                key: _redact(value) if isinstance(value, str) else value
                for key, value in record.args.items()
            }
        if record.exc_info and not record.exc_text:
            record.exc_text = "".join(traceback.format_exception(*record.exc_info)).rstrip()
        if record.exc_text:
            record.exc_text = _redact(record.exc_text)
        if record.stack_info:
            record.stack_info = _redact(record.stack_info)
        return True


def install_token_redaction() -> None:
    """Install before serving requests; repeated application imports are safe."""
    for name in ("uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, TokenQueryRedactionFilter) for f in logger.filters):
            logger.addFilter(TokenQueryRedactionFilter())
