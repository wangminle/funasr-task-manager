"""API Token authentication for the ASR Task Manager.

MVP implementation: static token-to-user mapping from configuration.
Future: JWT with proper token issuance and expiry.
"""

from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from app.observability.logging import get_logger

logger = get_logger(__name__)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

_TOKEN_USER_MAP: dict[str, str] = {
    "dev-token-user1": "user1",
    "dev-token-user2": "user2",
    "dev-token-admin": "admin",
}
_AUTH_ENABLED = False


def configure_auth(token_map: dict[str, str] | None = None, enabled: bool = True) -> None:
    """Configure authentication. Call during app startup."""
    global _TOKEN_USER_MAP, _AUTH_ENABLED
    if token_map is not None:
        _TOKEN_USER_MAP = token_map
    _AUTH_ENABLED = enabled


def is_auth_enabled() -> bool:
    return _AUTH_ENABLED


async def verify_token(api_key: str | None = Security(api_key_header)) -> str:
    """Verify API token and return user_id.
    
    When auth is disabled, returns 'default_user' for backward compatibility.
    """
    if not _AUTH_ENABLED:
        return "default_user"
    
    if api_key is None:
        raise HTTPException(status_code=401, detail="Missing API key. Provide X-API-Key header.")
    
    user_id = _TOKEN_USER_MAP.get(api_key)
    if user_id is None:
        logger.warning("auth_failed", reason="invalid_token")
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    return user_id


def get_admin_user_ids() -> set[str]:
    """Return set of admin user IDs."""
    return {"admin"}


async def verify_admin(api_key: str | None = Security(api_key_header)) -> str:
    """Verify that the caller is an admin."""
    user_id = await verify_token(api_key)
    if _AUTH_ENABLED and user_id not in get_admin_user_ids():
        raise HTTPException(status_code=403, detail="Admin access required")
    return user_id
