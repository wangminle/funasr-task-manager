"""API Token authentication for the ASR Task Manager.

MVP implementation: static token-to-user mapping from configuration.
Future: JWT with proper token issuance and expiry.

Security policy: auth is **disabled** only when explicitly configured so.
In production, set ASR_AUTH_ENABLED=true and provide token mappings.
"""

from fastapi import HTTPException, Query, Security
from fastapi.security import APIKeyHeader

from app.observability.logging import get_logger

logger = get_logger(__name__)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

_TOKEN_USER_MAP: dict[str, str] = {}
_AUTH_ENABLED = False
_AUTH_INITIALIZED = False


def configure_auth(token_map: dict[str, str] | None = None, enabled: bool = True) -> None:
    """Configure authentication. Must be called during app startup."""
    global _TOKEN_USER_MAP, _AUTH_ENABLED, _AUTH_INITIALIZED
    if token_map is not None:
        _TOKEN_USER_MAP = token_map
    _AUTH_ENABLED = enabled
    _AUTH_INITIALIZED = True

    if not enabled:
        logger.warning(
            "auth_disabled",
            hint="Authentication is DISABLED. Set ASR_AUTH_ENABLED=true for production.",
        )

    dev_token_prefixes = ("dev-token-", "test-token-")
    dev_tokens = [t for t in _TOKEN_USER_MAP if any(t.startswith(p) for p in dev_token_prefixes)]
    if dev_tokens and enabled:
        logger.warning(
            "auth_dev_tokens_detected",
            count=len(dev_tokens),
            hint="Development tokens detected in production auth config. Remove them.",
        )

    logger.info("auth_configured", enabled=enabled, token_count=len(_TOKEN_USER_MAP))


def init_auth_from_settings() -> None:
    """Initialize auth module from app settings. Called once during lifespan."""
    from app.config import settings
    token_map = None
    if settings.auth_tokens:
        token_map = {}
        for pair in settings.auth_tokens.split(","):
            pair = pair.strip()
            if ":" in pair:
                token, user = pair.split(":", 1)
                token_map[token.strip()] = user.strip()
    configure_auth(token_map=token_map, enabled=settings.auth_enabled)


def is_auth_enabled() -> bool:
    return _AUTH_ENABLED


async def verify_token(
    api_key: str | None = Security(api_key_header),
    token: str | None = Query(None, alias="token", include_in_schema=False),
) -> str:
    """Verify API token and return user_id.

    Accepts token via X-API-Key header or ?token= query param (for SSE/EventSource).
    When auth is disabled, returns 'default_user' for backward compatibility.
    """
    if not _AUTH_INITIALIZED:
        init_auth_from_settings()

    if not _AUTH_ENABLED:
        return "default_user"

    effective_key = api_key or token
    if effective_key is None:
        raise HTTPException(status_code=401, detail="Missing API key. Provide X-API-Key header or ?token= param.")

    user_id = _TOKEN_USER_MAP.get(effective_key)
    if user_id is None:
        logger.warning("auth_failed", reason="invalid_token")
        raise HTTPException(status_code=401, detail="Invalid API key")

    return user_id


def get_admin_user_ids() -> set[str]:
    """Return set of admin user IDs."""
    return {"admin"}


async def verify_admin(
    api_key: str | None = Security(api_key_header),
    token: str | None = Query(None, alias="token", include_in_schema=False),
) -> str:
    """Verify that the caller is an admin."""
    user_id = await verify_token(api_key, token)
    if _AUTH_ENABLED and user_id not in get_admin_user_ids():
        raise HTTPException(status_code=403, detail="Admin access required")
    return user_id
