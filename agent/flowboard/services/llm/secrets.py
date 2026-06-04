"""Local secret storage for the multi-LLM provider layer.

Schema of ``~/.flowboard/secrets.json``:

```json
{
  "apiKeys": {"openai": "sk-..."},
  "activeProviders": {
    "auto_prompt": "claude",
    "vision": "gemini",
    "planner": "claude"
  }
}
```

Stored as plain JSON with file mode ``0o600`` (owner read/write only).
Single-user local app — OS-level file permissions are sufficient. We
deliberately don't encrypt; encryption adds a key-management surface
area without real benefit when the only attacker that matters has
already won (root on this user's box).

Writes are atomic (`tmp + replace`) so a crash mid-write can't corrupt
the file — readers either see the old contents or the new contents,
never a half-written file.
"""
from __future__ import annotations

import json
import logging
import os
import contextvars
from pathlib import Path
from typing import Optional

from flowboard.db import get_auth_session
from flowboard.db.models import UserAccount

logger = logging.getLogger(__name__)

current_user_uid_var = contextvars.ContextVar("current_user_uid", default=None)

_DEFAULT_PATH = Path.home() / ".flowboard" / "secrets.json"


def _path() -> Path:
    """Indirection so tests can monkeypatch the location.

    Tests typically set ``FLOWBOARD_SECRETS_PATH`` to a tmp file. Production
    callers leave the env var unset and the default ``~/.flowboard/secrets.json``
    applies.
    """
    override = os.environ.get("FLOWBOARD_SECRETS_PATH")
    return Path(override) if override else _DEFAULT_PATH


def _is_multi_tenant() -> bool:
    """True when Firebase is initialized → we're in multi-user web mode.
    In this mode, secrets must come from per-user SQLite, never the global file.
    """
    if os.getenv("TESTING") == "true":
        return False
    try:
        import firebase_admin
        return len(firebase_admin._apps) > 0
    except ImportError:
        return False


def read() -> dict:
    """Load secrets document.

    Multi-tenant mode (Firebase initialized):
      - If current_user_uid is set → read from SQLite per-user record.
      - If uid is None (unauthenticated request) → return empty dict.
        NEVER fall back to the global file — that would leak one user's
        keys to another user's session.

    Single-user / test mode (no Firebase):
      - Fall back to ~/.flowboard/secrets.json for backward compat.
    """
    uid = current_user_uid_var.get()
    multi = _is_multi_tenant()

    if uid:
        # Authenticated user → always read from their DB record
        with get_auth_session() as session:
            user_acc = session.get(UserAccount, uid)
            if user_acc:
                doc = user_acc.llm_secrets or {}
                has_keys = bool((doc.get("apiKeys") or {}))
                logger.debug("secrets.read: uid=%s → DB hit, has_keys=%s", uid, has_keys)
                return doc
        logger.debug("secrets.read: uid=%s → no UserAccount row, returning empty", uid)
        return {}

    if multi:
        # Firebase is running but no uid → unauthenticated request.
        # Return empty to prevent leaking the global secrets file.
        logger.debug("secrets.read: uid=None, multi_tenant=True → returning empty (no leak)")
        return {}

    # No Firebase, no uid → single-user / test mode → use file
    p = _path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("secrets: file unreadable, treating as empty (%s)", exc)
        return {}


def write(payload: dict) -> None:
    """Write secrets. Multi-tenant aware — same isolation rules as read()."""
    uid = current_user_uid_var.get()
    if uid:
        with get_auth_session() as session:
            user_acc = session.get(UserAccount, uid)
            if user_acc:
                user_acc.llm_secrets = payload
                session.add(user_acc)
                session.commit()
        return

    if _is_multi_tenant():
        # Firebase is running but no uid → refuse to write to global file.
        logger.warning("secrets: write() called in multi-tenant mode without uid, ignoring")
        return

    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    # chmod BEFORE replace so the final file is never group/world-readable
    # even momentarily on filesystems that preserve permissions on rename.
    os.chmod(tmp, 0o600)
    tmp.replace(p)


# ── API key helpers ────────────────────────────────────────────────────

def get_api_key(provider: str) -> Optional[str]:
    """None if the key is unset OR if the file doesn't exist."""
    doc = read()
    keys = doc.get("apiKeys") or {}
    val = keys.get(provider)
    return val if isinstance(val, str) and val else None


def set_api_key(provider: str, key: Optional[str]) -> None:
    """Set or clear (key=None) a provider's API key.

    Clearing removes the entry entirely so ``get_api_key`` returns None
    cleanly without falsy-empty-string ambiguity.
    """
    doc = read()
    keys = dict(doc.get("apiKeys") or {})
    if key is None or not key:
        keys.pop(provider, None)
    else:
        keys[provider] = key
    doc["apiKeys"] = keys
    write(doc)


# ── Active-providers helpers ───────────────────────────────────────────

# Features the UI configures. Order matters only for display; iteration
# order in this module is deterministic on Python 3.7+.
_FEATURES: tuple[str, ...] = ("auto_prompt", "vision", "planner")


def read_active_providers() -> dict[str, str]:
    """Return ``{feature: provider_name}`` for features the user has
    explicitly picked. No defaults — missing keys are absent.

    Callers must handle the missing case (a feature with no provider
    pinned can't dispatch). The HTTP layer surfaces this via the
    ``configured`` flag on ``GET /api/llm/config``; the dispatch layer
    raises ``LLMError`` so the user sees a clear "open settings" message
    instead of silently falling back to a provider they didn't pick.
    """
    doc = read()
    saved = doc.get("activeProviders") or {}
    if not isinstance(saved, dict):
        return {}
    return {k: v for k, v in saved.items() if isinstance(v, str) and v}


def is_active_providers_configured() -> bool:
    """True when the user has completed the AI Provider setup flow.

    Single-provider model: every feature must be pinned AND all three
    must point at the same provider. Mixed config (legacy hand-edits
    or older versions that allowed per-feature) returns False so the
    forced-setup gate prompts the user to consolidate.
    """
    saved = read_active_providers()
    if not all(f in saved for f in _FEATURES):
        return False
    values = {saved[f] for f in _FEATURES}
    return len(values) == 1


def set_feature_provider(feature: str, provider: str) -> None:
    """Pin one feature to one provider. Caller validates names."""
    doc = read()
    saved = dict(doc.get("activeProviders") or {})
    saved[feature] = provider
    doc["activeProviders"] = saved
    write(doc)
