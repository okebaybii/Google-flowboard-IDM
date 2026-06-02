from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel, Column, JSON


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Board(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    created_at: datetime = Field(default_factory=_utcnow)


class Node(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    board_id: int = Field(foreign_key="board.id", index=True)
    short_id: str = Field(index=True)
    type: str
    x: float = 0.0
    y: float = 0.0
    w: float = 240.0
    h: float = 160.0
    data: dict = Field(default_factory=dict, sa_column=Column(JSON))
    status: str = "idle"
    created_at: datetime = Field(default_factory=_utcnow)


class Edge(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    board_id: int = Field(foreign_key="board.id", index=True)
    source_id: int = Field(foreign_key="node.id")
    target_id: int = Field(foreign_key="node.id")
    kind: str = "ref"
    # Per-edge variant pin: when the source node holds multiple variants
    # (`data.mediaIds`), this index selects WHICH variant feeds the
    # downstream as a reference. None = "fall back to the source's
    # active mediaId" (the natural single-variant case).
    #
    # Why per-edge instead of expanding all variants on the wire: each
    # variant of the same upstream produces a SEPARATE Flow API call
    # (Flow doesn't bind output[i] to input[i] when both are
    # multi-variant). Pinning lets the user say "use variant 2 for
    # downstream A, variant 3 for downstream B" with two clicks; the
    # edge UI surfaces the pinned index so the binding stays visible.
    source_variant_idx: Optional[int] = None


class Request(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    node_id: Optional[int] = Field(default=None, foreign_key="node.id", index=True)
    type: str
    params: dict = Field(default_factory=dict, sa_column=Column(JSON))
    status: str = "queued"
    result: dict = Field(default_factory=dict, sa_column=Column(JSON))
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    finished_at: Optional[datetime] = None


class Asset(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    # node_id is optional — assets can arrive from TRPC before any node
    # binding (e.g. the user browses an old Flow project).
    node_id: Optional[int] = Field(default=None, foreign_key="node.id", index=True)
    kind: str  # image | video | thumbnail
    # Media id (the hex uuid from Google Flow). Unique so ingest can upsert.
    uuid_media_id: Optional[str] = Field(default=None, index=True, unique=True)
    # Latest captured signed GCS URL (expires — refreshed when user reopens
    # Flow tab).
    url: Optional[str] = None
    local_path: Optional[str] = None
    mime: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)


class MediaProjectMapping(SQLModel, table=True):
    """Cross-project media re-upload cache.

    Flow scopes mediaIds to the project they were uploaded in — a
    ref_media_id from project A is unknown to project B even though we
    have the bytes cached locally. When a dispatch needs to reference
    media from another project (e.g. a cross-board Reference reused on
    a different board), we re-upload the bytes under the target project
    and record the (original, project) → project-local mapping here so
    subsequent dispatches skip the upload round-trip.

    Each row says: "bytes of `original_media_id` are also available
    under `project_id` as `project_local_media_id`". Unique on
    (original_media_id, project_id) — composite index in __table_args__.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    original_media_id: str = Field(index=True)
    project_id: str = Field(index=True)
    project_local_media_id: str
    created_at: datetime = Field(default_factory=_utcnow)
    __table_args__ = (
        UniqueConstraint(
            "original_media_id", "project_id",
            name="uq_media_project_mapping",
        ),
    )


class Reference(SQLModel, table=True):
    """User-curated saved media for cross-board reuse.

    Distinct from Asset (auto-managed cache index). Each Reference
    points at one media_id and snapshots enough metadata to spawn a
    brand-new visual_asset node in any board without re-vision or
    re-upload.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    media_id: str = Field(index=True, unique=True)
    url: Optional[str] = None
    label: str = ""
    kind: str  # "image" | "character" | "visual_asset" | "storyboard_shot"
    ai_brief: Optional[str] = None
    aspect_ratio: Optional[str] = None
    tags: list = Field(default_factory=list, sa_column=Column(JSON))
    pinned: bool = False
    position: int = 0
    source_board_id: Optional[int] = Field(default=None, foreign_key="board.id", index=True)
    source_node_short_id: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)


class ChatMessage(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    board_id: int = Field(foreign_key="board.id", index=True)
    role: str  # user | assistant | system
    content: str
    mentions: list = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_utcnow)


class Plan(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    board_id: int = Field(foreign_key="board.id", index=True)
    spec: dict = Field(default_factory=dict, sa_column=Column(JSON))
    status: str = "draft"  # draft | approved | running | done | failed
    created_at: datetime = Field(default_factory=_utcnow)


class PlanRevision(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    plan_id: int = Field(foreign_key="plan.id", index=True)
    rev_no: int
    spec: dict = Field(default_factory=dict, sa_column=Column(JSON))
    edits: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_utcnow)


class PipelineRun(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    plan_id: int = Field(foreign_key="plan.id", index=True)
    status: str = "pending"  # pending | running | done | failed
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error: Optional[str] = None


class BoardFlowProject(SQLModel, table=True):
    """1:1 link between a local board and a Google Flow project_id.

    Kept as a separate table so we don't have to migrate the Board schema.
    Paygate tier is loaded realtime from the extension via /api/auth/me,
    not persisted here — the binding is purely about project identity.
    """
    board_id: int = Field(primary_key=True, foreign_key="board.id")
    flow_project_id: str
    created_at: datetime = Field(default_factory=_utcnow)


class SocialAccount(SQLModel, table=True):
    """OAuth credentials for social media platforms.
    
    Stores access tokens and refresh tokens for Facebook, TikTok, YouTube, etc.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(index=True)  # facebook | tiktok | youtube | instagram
    account_id: str = Field(index=True)  # Platform-specific account/page ID
    access_token: str  # OAuth access token
    refresh_token: Optional[str] = None  # For platforms that support refresh
    token_expires_at: Optional[datetime] = None
    account_name: Optional[str] = None  # Display name (page name, username, etc.)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    __table_args__ = (
        UniqueConstraint(
            "platform", "account_id",
            name="uq_social_account",
        ),
    )


class ScheduledPost(SQLModel, table=True):
    """Scheduled posts for social media platforms.
    
    Tracks when and where to post assets (videos, images).
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    asset_id: int = Field(foreign_key="asset.id", index=True)
    social_account_id: int = Field(foreign_key="socialaccount.id", index=True)
    platform: str  # facebook | tiktok | youtube | instagram
    caption: str = ""  # Post caption/description
    scheduled_time: datetime  # When to post
    status: str = "pending"  # pending | posted | failed | cancelled
    posted_url: Optional[str] = None  # URL to the posted content
    error_message: Optional[str] = None  # Error details if failed
    created_at: datetime = Field(default_factory=_utcnow)
    posted_at: Optional[datetime] = None


class SocialBlock(SQLModel, table=True):
    """Social Media Block for scheduling posts across platforms.
    
    Allows users to configure social media posting with content,
    platform selection, and scheduling.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    node_id: int = Field(foreign_key="node.id", index=True)
    board_id: int = Field(foreign_key="board.id", index=True)
    
    # Platforms to post to (JSON list: ["facebook", "tiktok", "youtube", "instagram"])
    platforms: str = Field(default="[]", sa_column=Column(JSON))
    
    # Content configuration
    content_type: str = "manual"  # manual | ai_generated | from_connected
    content: str = ""  # Post content/caption
    ai_prompt: Optional[str] = None  # Prompt if AI generated
    
    # Scheduling
    scheduled_time: Optional[datetime] = None
    is_recurring: bool = False
    recurrence_pattern: Optional[str] = None  # daily | weekly | monthly
    
    # Status tracking
    status: str = "draft"  # draft | scheduled | posted | failed
    facebook_post_id: Optional[str] = None  # Facebook post ID after posting
    
    # Metadata
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class SocialBlockPost(SQLModel, table=True):
    """Tracks individual posts from a Social Block.
    
    Records when a Social Block posts to each platform.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    social_block_id: int = Field(foreign_key="socialblock.id", index=True)
    platform: str  # facebook | tiktok | youtube | instagram
    
    # Post details
    content: str  # Actual content posted
    posted_url: Optional[str] = None  # URL to posted content
    
    # Status
    status: str = "pending"  # pending | posted | failed
    error_message: Optional[str] = None
    
    # Timestamps
    scheduled_time: datetime
    posted_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_utcnow)


class UserSession(SQLModel, table=True):
    """Tracks active user sessions for Firebase Auth and concurrency control.
    
    Ensures 1 account can only have 1 active device/browser session.
    """
    firebase_uid: str = Field(primary_key=True)
    active_session_id: str
    last_active_at: datetime = Field(default_factory=_utcnow)


class UserAccount(SQLModel, table=True):
    """Tracks registered user accounts and their activation/approval status."""
    firebase_uid: str = Field(primary_key=True)
    email: str
    is_approved: bool = Field(default=False)
    is_admin: bool = Field(default=False)
    created_at: datetime = Field(default_factory=_utcnow)
