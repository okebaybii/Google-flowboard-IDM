"""OpenAI provider — HTTP client for OpenAI's REST API."""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
from pathlib import Path
from typing import Optional

import httpx

from .base import LLMError
from . import secrets
from .cli_utils import (
    validate_prompt_size,
    validate_attachment_paths,
)

logger = logging.getLogger(__name__)

_API_URL = "https://api.openai.com/v1/chat/completions"
_DEFAULT_TIMEOUT = 90.0
_DEFAULT_TEXT_MODEL = "gpt-4o-mini"
_DEFAULT_VISION_MODEL = "gpt-4o"
_MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024


class OpenAIProvider:
    """Conforms to ``LLMProvider`` (structural typing)."""

    name: str = "openai"
    supports_vision: bool = True
    test_timeout_secs: float = 30.0

    def __init__(self) -> None:
        pass

    def reset_cache(self) -> None:
        """Testing hook + Settings panel rescan support."""
        pass

    # ── availability ──────────────────────────────────────────────────

    async def is_available(self) -> bool:
        """does the user have an API key configured?"""
        return bool(secrets.get_api_key("openai"))

    @property
    def mode(self) -> str:
        """Reported by /api/llm/providers so the UI knows which row state
        to render. Returns 'api' if configured, else 'none'."""
        return "api" if bool(secrets.get_api_key("openai")) else "none"

    # ── dispatch ──────────────────────────────────────────────────────

    async def run(
        self,
        user_prompt: str,
        *,
        system_prompt: Optional[str] = None,
        attachments: Optional[list[str]] = None,
        timeout: float = _DEFAULT_TIMEOUT,
        model: Optional[str] = None,
    ) -> str:
        """Invoke OpenAI Chat Completions API."""
        try:
            validate_prompt_size(user_prompt)
            if system_prompt:
                validate_prompt_size(system_prompt)
            validate_attachment_paths(attachments)
        except ValueError as exc:
            raise LLMError(f"Invalid input: {exc}") from exc

        key = secrets.get_api_key("openai")
        if not key:
            raise LLMError("OpenAI API key is missing. Please set it in Settings.")

        chosen_model = model or os.environ.get("FLOWBOARD_OPENAI_MODEL") or (
            _DEFAULT_VISION_MODEL if attachments else _DEFAULT_TEXT_MODEL
        )

        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        if attachments:
            content: list[dict] = [{"type": "text", "text": user_prompt}]
            for path in attachments:
                content.append(_image_url_block(path))
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": user_prompt})

        payload = {"model": chosen_model, "messages": messages}

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    _API_URL,
                    headers={
                        "authorization": f"Bearer {key}",
                        "content-type": "application/json",
                    },
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise LLMError(f"openai request timed out after {timeout}s") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"openai transport error: {exc}") from exc

        if resp.status_code != 200:
            raise LLMError(
                f"openai HTTP {resp.status_code}: {_safe_error_message(resp)}"
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMError("openai response was not JSON") from exc
        
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"openai response missing content: {data!r:.200}") from exc


# ── helpers ───────────────────────────────────────────────────────────

def _image_url_block(path: str) -> dict:
    p = Path(path)
    size = p.stat().st_size
    if size > _MAX_ATTACHMENT_BYTES:
        raise LLMError(
            f"attachment too large for openai: "
            f"{size // (1024 * 1024)}MB > 5MB cap"
        )
    mime = mimetypes.guess_type(path)[0] or "image/jpeg"
    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{b64}"},
    }


def _safe_error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return "(non-JSON body)"
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            msg = err.get("message")
            if isinstance(msg, str):
                return msg[:200]
        msg = body.get("message")
        if isinstance(msg, str):
            return msg[:200]
    return "(unrecognised body)"
