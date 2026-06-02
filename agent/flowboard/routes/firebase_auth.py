"""Firebase Authentication & Single-Session Concurrency Control Routes."""
import logging
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, Header, Request as FastAPIRequest
from pydantic import BaseModel
from sqlmodel import select
import firebase_admin
from firebase_admin import auth as firebase_auth

from flowboard.db import get_session
from flowboard.db.models import UserSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def verify_firebase_token(id_token: str) -> dict:
    """Verify Firebase ID token, supporting mock tokens for local development fallback."""
    try:
        # If Firebase app is not initialized or configured, support mock tokens
        if not firebase_admin._apps:
            if id_token.startswith("mock_"):
                uid = id_token.replace("mock_", "")
                return {"uid": uid, "email": f"{uid}@example.com"}
            raise HTTPException(
                status_code=401,
                detail="Firebase is not configured on this agent. Please set up FIREBASE_SERVICE_ACCOUNT or use a mock token 'mock_yourname'."
            )
            
        decoded_token = firebase_auth.verify_id_token(id_token)
        if not decoded_token.get("email_verified", False):
            raise HTTPException(
                status_code=401,
                detail="email_not_verified"
            )
        return decoded_token
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to verify Firebase token: {e}")
        raise HTTPException(status_code=401, detail="invalid_token")


class RegisterSessionRequest(BaseModel):
    id_token: str
    session_id: str


@router.post("/register-session")
def register_session(req: RegisterSessionRequest) -> dict:
    """Register a new active browser session ID for the authenticated Firebase user."""
    user_info = verify_firebase_token(req.id_token)
    uid = user_info["uid"]
    email = user_info.get("email", "")
    
    with get_session() as session:
        user_sess = session.get(UserSession, uid)
        if not user_sess:
            user_sess = UserSession(
                firebase_uid=uid,
                active_session_id=req.session_id,
                last_active_at=datetime.now(timezone.utc)
            )
        else:
            user_sess.active_session_id = req.session_id
            user_sess.last_active_at = datetime.now(timezone.utc)
        session.add(user_sess)
        session.commit()
        
    logger.info(f"Registered new active session {req.session_id} for user {email} ({uid})")
    return {"ok": True, "uid": uid, "email": email}


@router.get("/heartbeat")
def heartbeat(
    x_session_id: str = Header(..., alias="X-Session-ID"),
    authorization: str = Header(...)
) -> dict:
    """Verify that the browser session is still the active one (has not been overtaken)."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="invalid_auth_header")
    id_token = authorization.split(" ")[1]
    
    user_info = verify_firebase_token(id_token)
    uid = user_info["uid"]
    
    with get_session() as session:
        user_sess = session.get(UserSession, uid)
        if not user_sess or user_sess.active_session_id != x_session_id:
            logger.warning(f"Session conflict detected for user {uid}. DB session: {user_sess.active_session_id if user_sess else 'None'}, Request session: {x_session_id}")
            raise HTTPException(status_code=401, detail="session_conflict")
            
        # Update heartbeat timestamp
        user_sess.last_active_at = datetime.now(timezone.utc)
        session.add(user_sess)
        session.commit()
        
    return {"ok": True}


async def check_active_session_globally(
    request: FastAPIRequest,
    x_session_id: Optional[str] = Header(None, alias="X-Session-ID"),
    authorization: Optional[str] = Header(None)
):
    """Global dependency to check active session ID for all API endpoints."""
    path = request.url.path
    # Skip non-API endpoints, health checks, and session registration
    if not path.startswith("/api") or path in (
        "/api/health",
        "/api/auth/register-session",
        "/api/ext/callback",
    ):
        return

    if not authorization or not x_session_id:
        raise HTTPException(status_code=401, detail="missing_auth_headers")

    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="invalid_auth_header")

    id_token = authorization.split(" ")[1]
    user_info = verify_firebase_token(id_token)
    uid = user_info["uid"]

    with get_session() as session:
        user_sess = session.get(UserSession, uid)
        if not user_sess or user_sess.active_session_id != x_session_id:
            logger.warning(f"Session conflict blocked request to {path} for user {uid}.")
            raise HTTPException(status_code=401, detail="session_conflict")
