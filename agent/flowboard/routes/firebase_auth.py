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
from flowboard.db.models import UserSession, UserAccount

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
        return decoded_token
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
    
    has_firebase = len(firebase_admin._apps) > 0
    
    with get_session() as session:
        user_acc = session.get(UserAccount, uid)
        
        # If it's a new account, we register it locally
        if not user_acc:
            # Check if this is the first user in the database
            first_user = session.exec(select(UserAccount)).first() is None
            
            # Disable subsequent users in Firebase Console so the admin must enable them
            if has_firebase and not first_user:
                try:
                    firebase_auth.update_user(uid, disabled=True)
                    logger.info(f"Programmatically disabled new Firebase user {email} ({uid}) pending Admin activation.")
                except Exception as e:
                    logger.error(f"Failed to programmatically disable Firebase user: {e}")
                    
            user_acc = UserAccount(
                firebase_uid=uid,
                email=email,
                is_approved=True if first_user else False,
                is_admin=True if first_user else False
            )
            session.add(user_acc)
            session.commit()
            session.refresh(user_acc)
            logger.info(f"Registered local UserAccount {email} (is_approved: {user_acc.is_approved}, is_admin: {user_acc.is_admin})")
            
        # Verify disabled status directly in Firebase Console (source of truth)
        if has_firebase:
            try:
                fb_user = firebase_auth.get_user(uid)
                if fb_user.disabled:
                    if user_acc.is_approved:
                        user_acc.is_approved = False
                        session.add(user_acc)
                        session.commit()
                    logger.warning(f"Login blocked: User {email} ({uid}) is disabled in Firebase Console.")
                    raise HTTPException(status_code=403, detail="account_not_approved")
                else:
                    # Sync local state if Admin enabled them in Firebase Console
                    if not user_acc.is_approved:
                        user_acc.is_approved = True
                        session.add(user_acc)
                        session.commit()
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to check Firebase user disabled state: {e}")
                raise HTTPException(status_code=403, detail="account_not_approved")
                
        elif not user_acc.is_approved:
            logger.warning(f"Login blocked (Mock mode): User {email} ({uid}) is not approved.")
            raise HTTPException(status_code=403, detail="account_not_approved")
            
        # 2. Manage session concurrency
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
    return {
        "ok": True, 
        "uid": uid, 
        "email": email, 
        "is_admin": user_acc.is_admin, 
        "is_approved": user_acc.is_approved
    }


@router.get("/heartbeat")
def heartbeat(
    x_session_id: str = Header(..., alias="X-Session-ID"),
    authorization: str = Header(...)
) -> dict:
    """Verify that the browser session is still active and account is approved."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="invalid_auth_header")
    id_token = authorization.split(" ")[1]
    
    user_info = verify_firebase_token(id_token)
    uid = user_info["uid"]
    has_firebase = len(firebase_admin._apps) > 0
    
    with get_session() as session:
        # Check approval status in Firebase Console directly
        if has_firebase:
            try:
                fb_user = firebase_auth.get_user(uid)
                if fb_user.disabled:
                    raise HTTPException(status_code=403, detail="account_not_approved")
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to verify Firebase status in heartbeat: {e}")
                raise HTTPException(status_code=403, detail="account_not_approved")
        else:
            user_acc = session.get(UserAccount, uid)
            if not user_acc or not user_acc.is_approved:
                raise HTTPException(status_code=403, detail="account_not_approved")

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
    """Global dependency to check active session ID and admin approval for all API endpoints."""
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
    has_firebase = len(firebase_admin._apps) > 0

    with get_session() as session:
        # Verify approval status in Firebase Console directly
        if has_firebase:
            try:
                fb_user = firebase_auth.get_user(uid)
                if fb_user.disabled:
                    raise HTTPException(status_code=403, detail="account_not_approved")
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to check Firebase user disabled state globally: {e}")
                raise HTTPException(status_code=403, detail="account_not_approved")
        else:
            user_acc = session.get(UserAccount, uid)
            if not user_acc or not user_acc.is_approved:
                raise HTTPException(status_code=403, detail="account_not_approved")

        user_sess = session.get(UserSession, uid)
        if not user_sess or user_sess.active_session_id != x_session_id:
            logger.warning(f"Session conflict blocked request to {path} for user {uid}.")
            raise HTTPException(status_code=401, detail="session_conflict")
