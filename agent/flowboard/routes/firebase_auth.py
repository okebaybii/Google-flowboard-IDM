"""Firebase Authentication & Single-Session Concurrency Control Routes."""
import logging
import os
import zipfile
import io
from datetime import datetime, timezone
from typing import Optional
from pathlib import Path
from fastapi import APIRouter, HTTPException, Depends, Header, Request as FastAPIRequest
from pydantic import BaseModel
from sqlmodel import select, delete as sql_delete, update
import firebase_admin
from firebase_admin import auth as firebase_auth

from flowboard.db import get_auth_session
from flowboard.db.models import (
    UserSession,
    UserAccount,
    Board,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def verify_firebase_token(id_token: str) -> dict:
    """Verify Firebase ID token, supporting mock tokens for local development fallback."""
    if id_token and id_token.startswith("mock_"):
        uid = id_token.replace("mock_", "")
        return {"uid": uid, "email": f"{uid}@example.com"}

    try:
        # If Firebase app is not initialized or configured, and not a mock token, error out
        if not firebase_admin._apps:
            raise HTTPException(
                status_code=401,
                detail="Firebase is not configured on this agent. Please set up FIREBASE_SERVICE_ACCOUNT or use a mock token 'mock_yourname'."
            )
            
        decoded_token = firebase_auth.verify_id_token(id_token)
        return decoded_token
    except HTTPException:
        raise
    except Exception as e:
        err_msg = str(e)
        
        # Handle clock skew: "Token used too early" with small time difference
        if "Token used too early" in err_msg or "used too early" in err_msg:
            # Extract timestamps if present (format: "1780495832 < 1780495833")
            import re
            import time
            match = re.search(r'(\d+)\s*<\s*(\d+)', err_msg)
            if match:
                token_time = int(match.group(1))
                server_time = int(match.group(2))
                time_diff = server_time - token_time
                
                # Allow up to 5 seconds of clock skew
                if 0 < time_diff <= 5:
                    logger.warning(f"Clock skew detected ({time_diff}s), waiting and retrying")
                    # Wait for the time difference + small buffer, then retry
                    time.sleep(time_diff + 0.5)
                    try:
                        decoded_token = firebase_auth.verify_id_token(id_token)
                        return decoded_token
                    except Exception as retry_err:
                        logger.error(f"Retry after clock skew wait failed: {retry_err}")
        
        logger.error(f"Failed to verify Firebase token: {err_msg}")
        raise HTTPException(status_code=401, detail=f"invalid_token: {err_msg}")


class RegisterSessionRequest(BaseModel):
    id_token: str
    session_id: str


@router.post("/register-session")
def register_session(req: RegisterSessionRequest) -> dict:
    """Register a new active browser session ID for the authenticated Firebase user."""
    user_info = verify_firebase_token(req.id_token)
    uid = user_info["uid"]
    email = user_info.get("email", "")
    
    # Check Whitelist
    from flowboard.config import ALLOWED_EMAILS, ALLOWED_DOMAINS
    email_lower = email.lower() if email else ""
    is_whitelisted = True
    if ALLOWED_EMAILS or ALLOWED_DOMAINS:
        is_whitelisted = False
        if ALLOWED_EMAILS and email_lower in ALLOWED_EMAILS:
            is_whitelisted = True
        elif ALLOWED_DOMAINS:
            domain = email_lower.split("@")[-1] if "@" in email_lower else ""
            if domain in ALLOWED_DOMAINS:
                is_whitelisted = True
                
    if not is_whitelisted:
        logger.warning(f"Registration/Login blocked: {email} is not whitelisted.")
        raise HTTPException(status_code=403, detail="email_not_whitelisted")
    
    has_firebase = len(firebase_admin._apps) > 0
    
    is_admin = False
    is_approved = False
    
    with get_auth_session() as session:
        user_acc = session.get(UserAccount, uid)
        
        # Check Expiration
        if user_acc and user_acc.expires_at:
            now_utc = datetime.now(timezone.utc)
            expires_utc = user_acc.expires_at.replace(tzinfo=timezone.utc) if user_acc.expires_at.tzinfo is None else user_acc.expires_at
            if now_utc > expires_utc:
                if has_firebase:
                    try:
                        firebase_auth.update_user(uid, disabled=True)
                    except Exception as e:
                        logger.error(f"Failed to auto-disable expired user {email}: {e}")
                user_acc.is_approved = False
                session.add(user_acc)
                session.commit()
                logger.warning(f"Login blocked: User {email} ({uid}) access has expired.")
                raise HTTPException(status_code=403, detail="account_expired")
        
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
            
            if user_acc.is_approved:
                logger.info(f"User {email} is approved.")
            
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
            
        # Extract fields before session closes
        is_admin = user_acc.is_admin
        is_approved = user_acc.is_approved

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
        "is_admin": is_admin, 
        "is_approved": is_approved
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
    
    from flowboard.services.llm.secrets import current_user_uid_var
    current_user_uid_var.set(uid)
    
    has_firebase = len(firebase_admin._apps) > 0
    
    with get_auth_session() as session:
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

        # Check expiration
        user_acc = session.get(UserAccount, uid)
        if user_acc and user_acc.expires_at:
            now_utc = datetime.now(timezone.utc)
            expires_utc = user_acc.expires_at.replace(tzinfo=timezone.utc) if user_acc.expires_at.tzinfo is None else user_acc.expires_at
            if now_utc > expires_utc:
                if has_firebase:
                    try:
                        firebase_auth.update_user(uid, disabled=True)
                    except Exception as e:
                        logger.error(f"Failed to auto-disable expired user {uid} in heartbeat: {e}")
                user_acc.is_approved = False
                session.add(user_acc)
                session.commit()
                raise HTTPException(status_code=403, detail="account_expired")

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
    if os.getenv("TESTING") == "true":
        return
        
    path = request.url.path
    # Skip non-API endpoints, health checks, and session registration
    if not path.startswith("/api") or path in (
        "/api/health",
        "/api/auth/register-session",
        "/api/auth/extension/download",
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
    
    from flowboard.services.llm.secrets import current_user_uid_var
    current_user_uid_var.set(uid)
    
    has_firebase = len(firebase_admin._apps) > 0

    with get_auth_session() as session:
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

        # Check expiration
        user_acc = session.get(UserAccount, uid)
        if user_acc and user_acc.expires_at:
            now_utc = datetime.now(timezone.utc)
            expires_utc = user_acc.expires_at.replace(tzinfo=timezone.utc) if user_acc.expires_at.tzinfo is None else user_acc.expires_at
            if now_utc > expires_utc:
                if has_firebase:
                    try:
                        firebase_auth.update_user(uid, disabled=True)
                    except Exception as e:
                        logger.error(f"Failed to auto-disable expired user {uid} globally: {e}")
                user_acc.is_approved = False
                session.add(user_acc)
                session.commit()
                raise HTTPException(status_code=403, detail="account_expired")

        user_sess = session.get(UserSession, uid)
        if not user_sess or user_sess.active_session_id != x_session_id:
            logger.warning(f"Session conflict blocked request to {path} for user {uid}.")
            raise HTTPException(status_code=401, detail="session_conflict")


# ── extension zip download and admin management ────────────────────────


@router.get("/extension/download")
def download_extension():
    """Package the extension directory on the fly and stream it as a ZIP file."""
    from flowboard.config import ROOT
    ext_dir = ROOT / "extension"
    if not ext_dir.exists():
        raise HTTPException(status_code=404, detail="Extension directory not found on server")
        
    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(ext_dir):
            for file in files:
                file_path = Path(root) / file
                archive_name = file_path.relative_to(ext_dir)
                zipf.write(file_path, archive_name)
                
    memory_file.seek(0)
    return StreamingResponse(
        memory_file,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=flowboard-extension.zip"}
    )


async def check_admin(
    authorization: str = Header(...)
) -> UserAccount:
    """Dependency to check if the requesting user is an admin."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="invalid_auth_header")
    id_token = authorization.split(" ")[1]
    user_info = verify_firebase_token(id_token)
    uid = user_info["uid"]
    
    with get_auth_session() as session:
        user_acc = session.get(UserAccount, uid)
        if not user_acc or not user_acc.is_admin:
            raise HTTPException(status_code=403, detail="not_admin")
        return user_acc


class AdminUserUpdatePayload(BaseModel):
    is_approved: bool
    is_admin: bool
    expires_at_iso: Optional[str] = None


@router.get("/admin/users")
def get_admin_users(admin: UserAccount = Depends(check_admin)) -> list:
    """List all registered user accounts."""
    with get_auth_session() as session:
        users = session.exec(select(UserAccount)).all()
        res = []
        for u in users:
            res.append({
                "firebase_uid": u.firebase_uid,
                "email": u.email,
                "is_approved": u.is_approved,
                "is_admin": u.is_admin,
                "expires_at": u.expires_at.isoformat() if u.expires_at else None,
                "created_at": u.created_at.isoformat() if u.created_at else None
            })
        return res


@router.put("/admin/users/{uid}")
def update_admin_user(
    uid: str,
    payload: AdminUserUpdatePayload,
    admin: UserAccount = Depends(check_admin)
) -> dict:
    """Update approval, admin rights, and expiration of a user."""
    has_firebase = len(firebase_admin._apps) > 0
    with get_auth_session() as session:
        user_acc = session.get(UserAccount, uid)
        if not user_acc:
            raise HTTPException(status_code=404, detail="user_not_found")
            
        if user_acc.firebase_uid == admin.firebase_uid:
            if not payload.is_admin or not payload.is_approved:
                raise HTTPException(status_code=400, detail="cannot_demote_self")

        user_acc.is_approved = payload.is_approved
        user_acc.is_admin = payload.is_admin
        
        if payload.expires_at_iso:
            try:
                iso_str = payload.expires_at_iso.replace('Z', '+00:00')
                user_acc.expires_at = datetime.fromisoformat(iso_str)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"invalid_date_format: {e}")
        else:
            user_acc.expires_at = None

        if has_firebase:
            try:
                firebase_auth.update_user(uid, disabled=not payload.is_approved)
                logger.info(f"Admin updated Firebase user {user_acc.email} disabled status to {not payload.is_approved}")
            except Exception as e:
                logger.error(f"Failed to update Firebase user disabled status: {e}")

        session.add(user_acc)
        session.commit()
        
    return {"ok": True}


@router.delete("/admin/users/{uid}")
def delete_admin_user(
    uid: str,
    admin: UserAccount = Depends(check_admin)
) -> dict:
    """Delete a user account and cascade-delete all their workspace boards/data."""
    if uid == admin.firebase_uid:
        raise HTTPException(status_code=400, detail="cannot_delete_self")
        
    has_firebase = len(firebase_admin._apps) > 0
    with get_auth_session() as session:
        user_acc = session.get(UserAccount, uid)
        if not user_acc:
            raise HTTPException(status_code=404, detail="user_not_found")

        user_sess = session.get(UserSession, uid)
        if user_sess:
            session.delete(user_sess)

        session.delete(user_acc)
        
        if has_firebase:
            try:
                firebase_auth.delete_user(uid)
                logger.info(f"Admin deleted user {user_acc.email} from Firebase Auth.")
            except Exception as e:
                logger.error(f"Failed to delete user from Firebase Auth: {e}")
                
        session.commit()
        
    return {"ok": True}


def get_current_user_uid(authorization: str = Header(...)) -> str:
    """FastAPI dependency to extract the Firebase UID from the Authorization header."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="invalid_auth_header")
    
    id_token = authorization.split(" ")[1]
    has_firebase = len(firebase_admin._apps) > 0
    
    # In mock mode, the ID token IS the UID
    if not has_firebase:
        return id_token
        
    user_info = verify_firebase_token(id_token)
    return user_info["uid"]

def get_optional_user_uid(authorization: str = Header(None)) -> str | None:
    """FastAPI dependency to optionally extract the Firebase UID if provided."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    
    id_token = authorization.split(" ")[1]
    has_firebase = len(firebase_admin._apps) > 0
    
    # In mock mode, the ID token IS the UID
    if not has_firebase:
        return id_token
        
    try:
        user_info = verify_firebase_token(id_token)
        return user_info["uid"]
    except:
        return None
