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
    logger.info("flowboard agent started (ws:9223 + worker + social-scheduler)")
    try:
        yield
    finally:
        worker.request_shutdown()
        try:
            await asyncio.wait_for(worker.drain(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("worker drain timed out")
        for t in (ws_task, worker_task, scheduler_task):
            t.cancel()
        await asyncio.gather(ws_task, worker_task, scheduler_task, return_exceptions=True)
        logger.info("flowboard agent stopped")


app = FastAPI(
    title="Flowboard Agent", 
    version="0.0.2", 
    lifespan=lifespan,
    dependencies=[Depends(check_active_session_globally)]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
