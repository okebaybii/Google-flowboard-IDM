"""Gemini provider — HTTP client for Google's Gemini Cloud API."""
from __future__ import annotations

import base64
import logging
import mimetypes
import os
from typing import Optional

import httpx

from .base import LLMError
from .cli_utils import (
    validate_prompt_size,
    validate_attachment_paths,
)
from . import secrets

logger = logging.getLogger(__name__)

# Stable production model
_DEFAULT_MODEL = "gemini-2.5-flash"


class GeminiProvider:
    """Conforms to ``LLMProvider`` (structural typing)."""

    name: str = "gemini"
    supports_vision: bool = True
    test_timeout_secs: float = 30.0  # API is faster and less prone to CLI's 429 backoff

    def __init__(self) -> None:
        pass

    # ── availability ──────────────────────────────────────────────────

    async def is_available(self) -> bool:
        """does the user have an API key configured?"""
        return bool(secrets.get_api_key("gemini"))

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
        """Invoke Gemini Cloud API."""
        try:
            validate_prompt_size(user_prompt)
            if system_prompt:
                validate_prompt_size(system_prompt)
            validate_attachment_paths(attachments)
        except ValueError as exc:
            raise LLMError(f"Invalid input: {exc}") from exc

        api_key = secrets.get_api_key("gemini")
        if not api_key:
            raise LLMError("Gemini API key is missing. Please set it in Settings.")

        model = os.environ.get("FLOWBOARD_GEMINI_MODEL") or _DEFAULT_MODEL
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

        parts = []
        if attachments:
            for path in attachments:
                mime_type, _ = mimetypes.guess_type(path)
                if not mime_type:
                    mime_type = "image/jpeg"
                try:
                    with open(path, "rb") as f:
                        data = base64.b64encode(f.read()).decode("utf-8")
                except Exception as exc:
                    raise LLMError(f"Failed to read attachment {path}: {exc}") from exc
                
                parts.append({
                    "inline_data": {
                        "mime_type": mime_type,
                        "data": data,
                    }
                })

        parts.append({"text": user_prompt})

        payload = {
            "contents": [
                {
                    "parts": parts
                }
            ]
        }
        if system_prompt:
            payload["system_instruction"] = {
                "parts": [{"text": system_prompt}]
            }

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json=payload, timeout=timeout)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text
            raise LLMError(f"Gemini API returned HTTP {exc.response.status_code}: {body}") from exc
        except httpx.RequestError as exc:
            raise LLMError(f"Gemini API request failed: {exc}") from exc
        except Exception as exc:
            raise LLMError(f"Gemini API error: {exc}") from exc

        try:
            candidates = data.get("candidates", [])
            if not candidates:
                # Handle block reasons
                prompt_feedback = data.get("promptFeedback", {})
                if "blockReason" in prompt_feedback:
                    raise LLMError(f"Prompt blocked by Gemini: {prompt_feedback['blockReason']}")
                raise LLMError("Gemini returned empty candidates list")
            
            response_parts = candidates[0].get("content", {}).get("parts", [])
            response_texts = [p.get("text", "") for p in response_parts if "text" in p]
            return "".join(response_texts).strip()
        except Exception as exc:
            if isinstance(exc, LLMError):
                raise
            raise LLMError(f"Failed to parse Gemini response: {exc}") from exc
