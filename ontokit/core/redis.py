"""Shared translation from the application Redis DSN to ARQ settings."""

from urllib.parse import unquote, urlparse

from arq.connections import RedisSettings

from ontokit.core.config import settings


def get_redis_settings() -> RedisSettings:
    """Preserve credentials, database, and TLS when constructing ARQ settings."""
    parsed = urlparse(str(settings.redis_url))
    return RedisSettings(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        database=int(parsed.path.lstrip("/") or "0"),
        username=unquote(parsed.username) if parsed.username else None,
        password=unquote(parsed.password) if parsed.password else None,
        ssl=parsed.scheme == "rediss",
    )


__all__ = ["get_redis_settings"]
