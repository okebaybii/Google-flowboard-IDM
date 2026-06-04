"""Claude provider — HTTP client for Anthropic's Claude API."""
from __future__ import annotations

import base64
import logging
import mimetypes
import os
from pathlib import Path
from typing import Optional

import httpx

from .base import LLMError
from .cli_utils import (
    validate_prompt_size,
    validate_attachment_paths,
)
from . import secrets

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "claude-3-5-sonnet-20241022"


class ClaudeProvider:
    """Conforms to ``LLMProvider`` (structural typing)."""

    name: str = "claude"
    supports_vision: bool = True
    test_timeout_secs: float = 30.0

    def __init__(self) -> None:
        pass

    # ── availability ──────────────────────────────────────────────────

    async def is_available(self) -> bool:
        """does the user have an API key configured?"""
        return bool(secrets.get_api_key("claude"))

    def reset_cache(self) -> None:
        """Testing hook + Settings panel rescan support."""
        pass

    # ── dispatch ──────────────────────────────────────────────────────

    async def run(
        self,
        user_prompt: str,
        *,
        system_prompt: Optional[str] = None,
        attachments: Optional[list[str]] = None,
        timeout: float = 90.0,
    ) -> str:
        """Invoke Anthropic Messages API."""
        try:
            validate_prompt_size(user_prompt)
            if system_prompt:
                validate_prompt_size(system_prompt)
            validate_attachment_paths(attachments)
        except ValueError as exc:
            raise LLMError(f"Invalid input: {exc}") from exc

        api_key = secrets.get_api_key("claude")
        if not api_key:
            raise LLMError("Claude API key is missing. Please set it in Settings.")

        model = os.environ.get("FLOWBOARD_CLAUDE_MODEL") or _DEFAULT_MODEL
        url = "https://api.anthropic.com/v1/messages"

        content = []
        if attachments:
            for path in attachments:
                mime_type, _ = mimetypes.guess_type(path)
                if not mime_type or not mime_type.startswith("image/"):
                    mime_type = "image/jpeg"
                try:
                    with open(path, "rb") as f:
                        data = base64.b64encode(f.read()).decode("ascii")
                except Exception as exc:
                    raise LLMError(f"Failed to read attachment {path}: {exc}") from exc
                
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime_type,
                        "data": data,
                    }
                })

        content.append({"type": "text", "text": user_prompt})

        payload = {
            "model": model,
            "max_tokens": 4096,
            "messages": [
                {
                    "role": "user",
                    "content": content
                }
            ]
        }
        if system_prompt:
            payload["system"] = system_prompt

        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json=payload, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            raise LLMError(f"Claude API request failed: {exc}") from exc
        except Exception as exc:
            raise LLMError(f"Claude API error: {exc}") from exc

        if resp.status_code != 200:
            try:
                err_data = resp.json()
                err_msg = err_data.get("error", {}).get("message", resp.text)
            except Exception:
                err_msg = resp.text
            raise LLMError(f"Claude API returned HTTP {resp.status_code}: {err_msg}")

        try:
            data = resp.json()
            content_blocks = data.get("content", [])
            if not content_blocks:
                raise LLMError("Claude returned empty content")
            
            response_texts = [block.get("text", "") for block in content_blocks if block.get("type") == "text"]
            return "".join(response_texts).strip()
        except Exception as exc:
            if isinstance(exc, LLMError):
                raise
            raise LLMError(f"Failed to parse Claude response: {exc}") from exc
