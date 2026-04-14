"""FastAPI dependency injection providers."""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.database import get_db_session
from app.auth.token import verify_token

DbSession = Annotated[AsyncSession, Depends(get_db_session)]
CurrentUser = Annotated[str, Depends(verify_token)]
