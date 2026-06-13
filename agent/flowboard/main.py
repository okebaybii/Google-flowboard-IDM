import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

from fastapi import FastAPI, HTTPException, Header, Depends, Request as FastAPIRequest
from fastapi.middleware.cors import CORSMiddleware

from flowboard.config import WS_HOST
from flowboard.db import get_session, init_db
from flowboard.db.models import Request
from flowboard.routes import activity, auth, boards, chat, edges, flow_projects, llm, media, nodes, oauth, plans, projects, prompt, social, social_block, upload, vision, video_assembly, firebase_auth
from flowboard.routes.firebase_auth import check_active_session_globally
from flowboard.routes import references as references_route
from flowboard.routes import requests as requests_route
from flowboard.services.flow_client import flow_client
from flowboard.services.ws_server import run_ws_server
from flowboard.worker.processor import get_worker
from flowboard.worker.social_scheduler import process_scheduled_posts

# Guard rail: the dedicated WS server is unauthenticated and would expose the
# callback secret to any process that can reach it. Refuse to boot if someone
# overrode WS_HOST to a non-loopback address.
if WS_HOST not in ("127.0.0.1", "localhost", "::1"):
    raise RuntimeError(
        f"FLOWBOARD_WS_HOST must be loopback (got {WS_HOST!r}); the extension WS "
        "is unauthenticated by design and must not be network-reachable."
    )

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def _recover_orphan_running_requests() -> int:
    """Mark any pre-existing 'running' requests as failed so a restart doesn't
    leave nodes polling a request that nobody is processing anymore."""
    from datetime import datetime, timezone
    from sqlmodel import select as _select

    touched = 0
    with get_session() as s:
        rows = s.exec(_select(Request).where(Request.status == "running")).all()
        for r in rows:
            r.status = "failed"
            r.error = "agent_restart_lost"
            r.finished_at = datetime.now(timezone.utc)
            s.add(r)
            touched += 1
        if touched:
            s.commit()
    return touched


async def _run_social_scheduler() -> None:
    """Background task that processes scheduled social media posts every minute."""
    while True:
        try:
            await process_scheduled_posts()
        except Exception as e:
            logger.error(f"Error in social scheduler: {e}")
        # Check every 60 seconds
        await asyncio.sleep(60)


async def _run_account_expiry_scheduler() -> None:
    """Background task that checks for expired accounts and disables them on Firebase every minute."""
    from datetime import datetime, timezone
    import firebase_admin
    from firebase_admin import auth as firebase_auth
    from flowboard.db.models import UserAccount
    from sqlmodel import select
    
    while True:
        try:
            has_firebase = len(firebase_admin._apps) > 0
            now = datetime.now(timezone.utc)
            with get_session() as session:
                stmt = select(UserAccount).where(
                    UserAccount.is_approved == True,
                    UserAccount.expires_at != None
                )
                users = session.exec(stmt).all()
                for u in users:
                    expires_utc = u.expires_at.replace(tzinfo=timezone.utc) if u.expires_at.tzinfo is None else u.expires_at
                    if now > expires_utc:
                        u.is_approved = False
                        session.add(u)
                        logger.warning(f"Background worker: User {u.email} ({u.firebase_uid}) has expired. Revoking approval.")
                        
                        if has_firebase:
                            try:
                                firebase_auth.update_user(u.firebase_uid, disabled=True)
                                logger.info(f"Background worker: Disabled expired user {u.email} in Firebase Auth.")
                            except Exception as fb_err:
                                logger.error(f"Background worker: Failed to disable user in Firebase: {fb_err}")
                session.commit()
        except Exception as e:
            logger.error(f"Error in account expiry scheduler: {e}")
            
        await asyncio.sleep(60)


def _auto_import_facebook_accounts() -> None:
    """Auto-import Facebook accounts from .env file."""
    import os
    from sqlmodel import select
    from flowboard.db.models import SocialAccount
    
    page_id = os.getenv("FB_PAGE__ID")
    page_token = os.getenv("FB_PAGE__ACCESS_TOKEN")
    page_name = "Facebook Page"
    
    if not page_id or not page_token:
        return
    
    try:
        with get_session() as session:
            # Check if account already exists
            existing = session.exec(
                select(SocialAccount).where(
                    SocialAccount.account_id == page_id,
                    SocialAccount.platform == "facebook"
                )
            ).first()
            
            if not existing:
                account = SocialAccount(
                    platform="facebook",
                    account_id=page_id,
                    access_token=page_token,
                    account_name=page_name,
                )
                session.add(account)
                session.commit()
                logger.info(f"✅ Auto-imported Facebook account: {page_name} ({page_id})")
            else:
                logger.info(f"ℹ️ Facebook account already exists: {page_name}")
    except Exception as e:
        logger.error(f"❌ Failed to auto-import Facebook account: {str(e)}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    
    # Initialize Firebase Admin SDK
    import os
    import firebase_admin
    from pathlib import Path
    from firebase_admin import credentials
    
    cred_path = os.getenv("FIREBASE_SERVICE_ACCOUNT") or "firebase-service-account.json"
    resolved_cred_path = Path(cred_path)
    if not resolved_cred_path.exists():
        # Fallback to checking relative to the agent folder parent
        fallback_path = Path(__file__).resolve().parent.parent / cred_path
        if fallback_path.exists():
            resolved_cred_path = fallback_path
            
    if resolved_cred_path.exists():
        try:
            cred = credentials.Certificate(str(resolved_cred_path))
            firebase_admin.initialize_app(cred)
            logger.info(f"✅ Firebase Admin SDK initialized successfully from {resolved_cred_path}.")
        except Exception as e:
            logger.error(f"❌ Failed to initialize Firebase Admin: {e}")
    else:
        logger.warning(f"⚠️ Firebase service account key not found at {cred_path}. Using mock authentication for development fallback.")

    _auto_import_facebook_accounts()  # Auto-import Facebook account
    recovered = _recover_orphan_running_requests()
    if recovered:
        logger.info("recovered %d orphan running request(s) → failed", recovered)
    worker = get_worker()
    ws_task = asyncio.create_task(run_ws_server(), name="ext-ws-server")
    worker_task = asyncio.create_task(worker.start(), name="request-worker")
    scheduler_task = asyncio.create_task(_run_social_scheduler(), name="social-scheduler")
    expiry_task = asyncio.create_task(_run_account_expiry_scheduler(), name="account-expiry-scheduler")
    logger.info("flowboard agent started (ws:9223 + worker + social-scheduler + expiry-scheduler)")
    try:
        yield
    finally:
        worker.request_shutdown()
        try:
            await asyncio.wait_for(worker.drain(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("worker drain timed out")
        for t in (ws_task, worker_task, scheduler_task, expiry_task):
            t.cancel()
        await asyncio.gather(ws_task, worker_task, scheduler_task, expiry_task, return_exceptions=True)
        logger.info("flowboard agent stopped")


app = FastAPI(
    title="Flowboard Agent", 
    version="0.0.2", 
    lifespan=lifespan,
    dependencies=[Depends(check_active_session_globally)]
)

# NOTE: the browser rejects `allow_origins=["*"]` together with
# `allow_credentials=True`. Flowboard only ever runs on loopback (the Vite dev
# server on :1234 and the bundled EXE serving the SPA from :8101), so list those
# origins explicitly to keep credentialed requests valid.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:1234",
        "http://127.0.0.1:1234",
        "http://localhost:8101",
        "http://127.0.0.1:8101",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def user_context_middleware(request: FastAPIRequest, call_next):
    authorization = request.headers.get("authorization")
    uid = None
    if authorization and authorization.startswith("Bearer "):
        id_token = authorization.split(" ")[1]
        try:
            from flowboard.routes.firebase_auth import verify_firebase_token
            user_info = verify_firebase_token(id_token)
            uid = user_info.get("uid")
        except Exception:
            pass

    from flowboard.services.llm.secrets import current_user_uid_var
    token = current_user_uid_var.set(uid)
    try:
        return await call_next(request)
    finally:
        current_user_uid_var.reset(token)

app.include_router(firebase_auth.router)
app.include_router(boards.router)
app.include_router(nodes.router)
app.include_router(edges.router)
app.include_router(chat.router)
app.include_router(projects.router)
app.include_router(flow_projects.router)
app.include_router(references_route.router)
app.include_router(requests_route.router)
app.include_router(media.bytes_router)
app.include_router(media.api_router)
app.include_router(upload.router)
app.include_router(plans.router)
app.include_router(vision.router)
app.include_router(prompt.router)
app.include_router(auth.router)
app.include_router(llm.router)
app.include_router(social.router)
app.include_router(oauth.router)
app.include_router(social_block.router)
app.include_router(activity.router)
app.include_router(video_assembly.router)


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "extension_connected": flow_client.connected,
        "ws_stats": flow_client.ws_stats,
    }


@app.post("/api/ext/callback")
async def ext_callback(
    body: FastAPIRequest,
    x_callback_secret: str | None = Header(default=None, alias="X-Callback-Secret"),
) -> dict:
    """HTTP callback for the extension to deliver API responses."""
    if not x_callback_secret or not hmac.compare_digest(
        x_callback_secret, flow_client.callback_secret
    ):
        raise HTTPException(status_code=401, detail="invalid callback secret")

    try:
        payload = await body.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json body")

    if not isinstance(payload, dict) or "id" not in payload:
        raise HTTPException(status_code=400, detail="missing id")

    matched = flow_client.resolve_callback(payload)
    return {"ok": matched}


# --- Serve Frontend (for standalone EXE build) ---
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import sys
import os

def get_frontend_dist_path():
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, "frontend_dist")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "frontend", "dist")

frontend_dist = get_frontend_dist_path()
if os.path.exists(frontend_dist) and os.path.exists(os.path.join(frontend_dist, "index.html")):
    # Serve assets directory statically
    assets_dir = os.path.join(frontend_dist, "assets")
    if os.path.exists(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")
        
    # Additional static files at root level (vite/react may output some)
    for root_file in ["vite.svg", "favicon.ico"]:
        file_path = os.path.join(frontend_dist, root_file)
        if os.path.exists(file_path):
            @app.get(f"/{root_file}", include_in_schema=False)
            def _serve_root_file(file=file_path):
                return FileResponse(file)

    @app.get("/{catchall:path}", include_in_schema=False)
    def serve_frontend_catchall(catchall: str):
        # Allow API calls to fail naturally with 404 instead of returning index.html
        if catchall.startswith("api/"):
            raise HTTPException(status_code=404, detail="API route not found")
        # For everything else, serve index.html to allow React Router to handle the URL
        return FileResponse(os.path.join(frontend_dist, "index.html"))
