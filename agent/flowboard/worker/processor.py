"""In-process worker that drains queued generation requests.

Scope for Run 3 (Phase 2 bridge): a single handler type `"proxy"` that
forwards `params = {url, method?, headers?, body?}` through the extension
via ``flow_client.api_request``. Further types (gen_image, gen_video,
upload_image, etc.) land in later runs once the full Flow protocol + captcha
round-trip is ported.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from flowboard.db import get_session
from flowboard.db.models import Request
from flowboard.services import media as media_service
from flowboard.services.flow_client import flow_client
from flowboard.services.flow_sdk import get_flow_sdk

logger = logging.getLogger(__name__)


# type → coroutine(params) → (result_dict, error_or_None)
Handler = Callable[[dict], Awaitable[tuple[dict, Optional[str]]]]


_ALLOWED_URL_PREFIXES: tuple[str, ...] = (
    "https://aisandbox-pa.googleapis.com/",
)


async def _handle_proxy(params: dict) -> tuple[dict, Optional[str]]:
    url = params.get("url")
    method = params.get("method", "POST")
    if not isinstance(url, str) or not url:
        return {}, "missing_url"
    # Defense-in-depth: refuse to proxy URLs outside the expected allowlist
    # even if the extension's own check was somehow bypassed.
    if not any(url.startswith(p) for p in _ALLOWED_URL_PREFIXES):
        return {}, "url_not_allowed"
    resp = await flow_client.api_request(
        url=url,
        method=method,
        headers=params.get("headers") or {},
        body=params.get("body"),
    )
    if not isinstance(resp, dict):
        return {"value": resp}, None
    if resp.get("error"):
        return resp, str(resp["error"])[:200]
    status = resp.get("status")
    if isinstance(status, int) and status >= 400:
        return resp, f"API_{status}"
    return resp, None


async def _handle_create_project(params: dict) -> tuple[dict, Optional[str]]:
    name = params.get("name") or params.get("title") or "Untitled"
    if not isinstance(name, str) or not name.strip():
        return {}, "missing_name"
    tool = params.get("tool", "PINHOLE")
    resp = await get_flow_sdk().create_project(name.strip(), tool)
    if resp.get("error"):
        return resp, str(resp["error"])[:200]
    return resp, None


async def _resolve_paygate_tier(params: dict) -> Optional[str]:
    """Resolve the account's paygate tier, auto-detecting it per account.

    Resolution priority:
      1. `params["paygate_tier"]` — caller intent stamped at dispatch.
      2. `flow_client.paygate_tier` — cached from the authoritative live
         `/v1/credits` lookup (populated on token capture).
      3. An on-demand live fetch — when the cache is cold but the extension
         has pushed a Bearer token, auto-detect the tier for THIS account
         instead of guessing.

    Returns ``None`` when the tier genuinely cannot be determined (no token
    yet). Callers MUST fail loud with ``paygate_tier_unknown`` rather than
    defaulting — a wrong tier silently downgrades Ultra accounts to Pro and
    stamps the bad value into request.params, which then feeds back through
    `_last_observed_paygate_tier_from_db()` and corrupts /api/auth/me for
    the rest of the session. This is the Phase-1 fail-loud contract.
    """
    tier = params.get("paygate_tier") or flow_client.paygate_tier
    if tier:
        return tier
    # Cache is cold — try to auto-detect from the authoritative per-account
    # source before giving up. No-ops (returns False) when no token is cached.
    try:
        if await flow_client.fetch_paygate_tier():
            return flow_client.paygate_tier
    except Exception as exc:  # noqa: BLE001
        logger.warning("paygate tier auto-detect failed: %s", exc)
    return None


async def _handle_gen_image(params: dict) -> tuple[dict, Optional[str]]:
    from flowboard.services.flow_sdk import is_valid_project_id

    prompt = params.get("prompt")
    project_id = params.get("project_id")
    if not isinstance(prompt, str) or not prompt.strip():
        return {}, "missing_prompt"
    if not isinstance(project_id, str) or not project_id.strip():
        return {}, "missing_project_id"
    project_id = project_id.strip()
    if not is_valid_project_id(project_id):
        return {}, "invalid_project_id"
    aspect = params.get("aspect_ratio") or "IMAGE_ASPECT_RATIO_LANDSCAPE"
    # Tier resolution: caller-stamped → cached → live auto-detect per account.
    # NO silent default — if the tier genuinely cannot be determined we fail
    # loud with `paygate_tier_unknown` (Phase-1 contract). The regressed
    # behaviour (default `PAYGATE_TIER_TWO`) silently mis-tiered accounts and
    # poisoned /api/auth/me for the rest of the session. See `_resolve_paygate_tier`.
    tier = await _resolve_paygate_tier(params)
    if not tier:
        return {}, "paygate_tier_unknown"
    # `ref_media_ids` is the broader name (any upstream image / character /
    # visual_asset feeds in as IMAGE_INPUT_TYPE_REFERENCE). Older callers used
    # `character_media_ids` — accept both.
    raw_ref_ids = params.get("ref_media_ids")
    if not isinstance(raw_ref_ids, list):
        raw_ref_ids = params.get("character_media_ids")
    ref_media_ids: Optional[list[str]] = None
    if isinstance(raw_ref_ids, list):
        cleaned = [m for m in raw_ref_ids if isinstance(m, str) and m]
        ref_media_ids = cleaned or None
    raw_count = params.get("variant_count")
    variant_count = 1
    if isinstance(raw_count, int) and raw_count > 0:
        variant_count = raw_count
    # Per-variant prompts (optional). When provided, each variant gets its
    # own text — used by auto-prompt batch mode so variants don't collapse
    # to the same stance.
    raw_prompts = params.get("prompts")
    per_variant_prompts: Optional[list[str]] = None
    if isinstance(raw_prompts, list):
        cleaned = [p for p in raw_prompts if isinstance(p, str) and p.strip()]
        per_variant_prompts = cleaned or None
    image_model = params.get("image_model")
    if not isinstance(image_model, str) or not image_model.strip():
        image_model = None
    resp = await get_flow_sdk().gen_image(
        prompt=prompt.strip(),
        project_id=project_id,
        aspect_ratio=aspect,
        paygate_tier=tier,
        ref_media_ids=ref_media_ids,
        variant_count=variant_count,
        prompts=per_variant_prompts,
        image_model=image_model,
    )
    if resp.get("error"):
        return resp, str(resp["error"])[:200]
    # Flow returns signed fifeUrls directly in the response — persist them
    # immediately so `/media/:id` can serve bytes without any extra round-trip.
    entries_with_urls = [
        e for e in (resp.get("media_entries") or []) if isinstance(e, dict) and e.get("url")
    ]
    if entries_with_urls:
        try:
            media_service.ingest_urls(entries_with_urls)
        except Exception:  # noqa: BLE001
            logger.exception("auto-ingest from gen_image response failed")
    
    # Extract media_ids from media_entries so the processor's node update
    # logic (lines 803-811) can populate node.data.mediaId/mediaIds correctly.
    # This matches the response structure from _handle_gen_video.
    media_ids = [
        e.get("media_id") for e in (resp.get("media_entries") or [])
        if isinstance(e, dict) and isinstance(e.get("media_id"), str)
    ]
    if media_ids:
        resp["media_ids"] = media_ids
    
    return resp, None


# Video polling knobs — overridable in tests. 5-minute hard deadline
# (30 cycles × 10s). When the budget runs out without all ops finishing
# the handler returns the ``timeout_waiting_video`` sentinel and the
# worker stamps the row as ``status='timeout'`` (distinct from
# ``failed``) so the UI can render it as a soft auto-cancel rather than
# a generation error.
VIDEO_POLL_INTERVAL_S = 10.0
VIDEO_POLL_MAX_CYCLES = 30


def _is_request_canceled(rid: Optional[int]) -> bool:
    """Return True iff the cancel endpoint flipped this row to canceled.

    Long-running handlers call this between polls so a user-initiated
    cancel takes effect mid-flight (we can't abort the Flow HTTP calls
    themselves, but we can stop polling and let _process_one keep the
    canceled status intact).
    """
    if not isinstance(rid, int):
        return False
    with get_session() as s:
        req = s.get(Request, rid)
        if req is None:
            return True
        return req.status == "canceled"


async def _extract_last_frame_and_upload(video_media_id: str, project_id: str) -> str:
    """Extracts the last frame of a video asset and uploads it to Flow as an image."""
    from sqlmodel import select
    from flowboard.db.models import Asset
    from flowboard.services.flow_sdk import get_flow_sdk
    import subprocess
    import tempfile
    import os
    import base64
    
    with get_session() as s:
        asset = s.exec(select(Asset).where(Asset.uuid_media_id == video_media_id)).first()
        if not asset or asset.kind != "video" or not asset.local_path:
            return video_media_id  # Not a video or no local path, return original

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp_out:
        out_path = tmp_out.name
    
    try:
        cmd = [
            "ffmpeg", "-y", "-sseof", "-0.1", "-i", asset.local_path,
            "-update", "1", "-q:v", "2", out_path
        ]
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        
        with open(out_path, "rb") as f:
            b64_data = base64.b64encode(f.read()).decode("utf-8")
            
        sdk = get_flow_sdk()
        # upload_image's parameter is `image_base64` and it returns
        # {raw, media_id} (media_id already extracted). Passing `b64_data`
        # raised TypeError against the real SDK, which the broad except below
        # swallowed — silently breaking the extend-video feature.
        resp = await sdk.upload_image(
            image_base64=b64_data,
            project_id=project_id,
            mime_type="image/jpeg",
            file_name=f"extend_{video_media_id}.jpg"
        )
        if "error" in resp:
            logger.error(f"Failed to upload extracted frame: {resp['error']}")
            return video_media_id

        new_media_id = resp.get("media_id")
        if not new_media_id:
            return video_media_id
            
        return new_media_id
    except Exception as e:
        logger.error(f"Failed to extract/upload last frame: {e}")
        return video_media_id
    finally:
        if os.path.exists(out_path):
            os.unlink(out_path)


async def _handle_gen_video(params: dict) -> tuple[dict, Optional[str]]:
    from flowboard.services.flow_sdk import is_valid_project_id

    prompt = params.get("prompt")
    project_id = params.get("project_id")
    start_media_id = params.get("start_media_id") or params.get("startMediaId")
    raw_starts = params.get("start_media_ids")
    start_media_ids: Optional[list[str]] = None
    if isinstance(raw_starts, list):
        cleaned = [m for m in raw_starts if isinstance(m, str) and m.strip()]
        start_media_ids = [m.strip() for m in cleaned] or None

    end_media_id = params.get("end_media_id") or params.get("endMediaId")
    raw_ends = params.get("end_media_ids")
    end_media_ids: Optional[list[str]] = None
    if isinstance(raw_ends, list):
        cleaned_ends = [m for m in raw_ends if isinstance(m, str) and m.strip()]
        end_media_ids = [m.strip() for m in cleaned_ends] or None

    if not isinstance(prompt, str) or not prompt.strip():
        return {}, "missing_prompt"
    if not isinstance(project_id, str) or not project_id.strip():
        return {}, "missing_project_id"
    project_id = project_id.strip()
    if not is_valid_project_id(project_id):
        return {}, "invalid_project_id"
    
    # Extract last frame if start_media_id points to a video
    if isinstance(start_media_id, str) and start_media_id.strip():
        start_media_id = await _extract_last_frame_and_upload(start_media_id.strip(), project_id)
        
    if start_media_ids:
        new_starts = []
        for mid in start_media_ids:
            new_starts.append(await _extract_last_frame_and_upload(mid, project_id))
        start_media_ids = new_starts

    # Either a single start_media_id OR a non-empty start_media_ids list.
    if start_media_ids is None and (
        not isinstance(start_media_id, str) or not start_media_id.strip()
    ):
        return {}, "missing_start_media_id"
    aspect = params.get("aspect_ratio") or "VIDEO_ASPECT_RATIO_LANDSCAPE"
    # Tier resolution — see _resolve_paygate_tier / _handle_gen_image for the
    # rationale. No silent default; an undeterminable tier is a hard error so
    # we never dispatch an Ultra user's video at the Pro checkpoint.
    tier = await _resolve_paygate_tier(params)
    if not tier:
        return {}, "paygate_tier_unknown"
    video_quality = params.get("video_quality")
    if not isinstance(video_quality, str) or not video_quality.strip():
        video_quality = None

    sdk = get_flow_sdk()
    dispatch = await sdk.gen_video(
        prompt=prompt.strip(),
        project_id=project_id,
        start_media_id=start_media_id.strip()
        if isinstance(start_media_id, str) and start_media_id.strip()
        else None,
        start_media_ids=start_media_ids,
        aspect_ratio=aspect,
        paygate_tier=tier,
        video_quality=video_quality,
        end_media_id=end_media_id.strip()
        if isinstance(end_media_id, str) and end_media_id.strip()
        else None,
        end_media_ids=end_media_ids,
    )
    if dispatch.get("error"):
        return dispatch, str(dispatch["error"])[:200]

    op_names = dispatch.get("operation_names") or []
    if not op_names:
        return dispatch, "no_operations_returned"
    # NEW low-priority models return workflows (`{name, primary_media_id}`)
    # instead of operations; the SDK surfaces them on `dispatch["workflows"]`
    # so we can route the poll to /v1/media/<id> instead of batchCheckAsync.
    workflows = dispatch.get("workflows") or None

    poll_attempts = 0
    last_poll: dict = {}
    done_by_name: dict[str, bool] = {name: False for name in op_names}
    entry_by_name: dict[str, dict] = {}
    op_errors: dict[str, str] = {}
    rid = params.get("__request_id")

    # Per-op resolution: each operation in the batch resolves
    # independently (success, content-filter rejection, or timeout). We
    # used to break the whole loop on the first per-op error, which
    # collapsed a 4-variant gen into a hard failure even when 3/4 clips
    # had already rendered. Now we let every op terminate on its own
    # and aggregate the outcome at the end so partial batches still
    # surface the variants that did succeed.
    while (
        poll_attempts < VIDEO_POLL_MAX_CYCLES
        and not all(done_by_name.values())
    ):
        await asyncio.sleep(VIDEO_POLL_INTERVAL_S)
        poll_attempts += 1
        if _is_request_canceled(rid):
            # User canceled mid-poll. Bail with the special error code
            # so _process_one knows to leave the row's canceled status
            # intact (the cancel endpoint already stamped finished_at +
            # error='canceled'). Any partial state we collected is
            # preserved on `result` for the detail viewer.
            return (
                {
                    "raw_dispatch": dispatch,
                    "last_poll": last_poll,
                    "operation_names": op_names,
                    "done": done_by_name,
                    "canceled": True,
                },
                "canceled",
            )
        last_poll = await sdk.check_async(op_names, workflows=workflows)
        if last_poll.get("error"):
            continue
        for op in last_poll.get("operations") or []:
            if not isinstance(op, dict):
                continue
            name = op.get("name")
            if not isinstance(name, str) or done_by_name.get(name, False):
                continue
            # Per-op terminal failure (e.g. content filter
            # PUBLIC_ERROR_UNSAFE_GENERATION / PUBLIC_ERROR_AUDIO_FILTERED).
            # Mark this op resolved-with-error and keep polling the rest.
            err = op.get("error")
            if isinstance(err, str) and err:
                done_by_name[name] = True
                op_errors[name] = err
                continue
            if op.get("done"):
                done_by_name[name] = True
                # Each op is expected to yield exactly one media entry
                # on success; capture the first valid one.
                for e in op.get("media_entries") or []:
                    if isinstance(e, dict) and e.get("media_id"):
                        entry_by_name[name] = e
                        break

    # Slots still unresolved after the max cycles — record as timeout
    # so the partial summary names them alongside any filter failures.
    for name in op_names:
        if not done_by_name.get(name) and name not in op_errors:
            op_errors[name] = "timeout_waiting_video"

    # Build positional outcome aligned to dispatch order. Slot i in
    # `media_ids` corresponds to slot i in the original
    # `start_media_ids` array, so the frontend can keep upstream-image
    # variant ↔ video-variant alignment even when middle slots fail.
    # `slot_errors` mirrors the same indexing — `None` for succeeded
    # slots, error code for blocked ones — so the detail viewer can
    # render the exact filter reason on the blocked tile without
    # having to know the internal Flow op-name keys.
    positional_ids: list[Optional[str]] = []
    slot_errors: list[Optional[str]] = []
    succeeded_entries: list[dict] = []
    for name in op_names:
        e = entry_by_name.get(name)
        if isinstance(e, dict) and isinstance(e.get("media_id"), str):
            positional_ids.append(e["media_id"])
            succeeded_entries.append(e)
            slot_errors.append(None)
        else:
            positional_ids.append(None)
            slot_errors.append(op_errors.get(name))

    success_count = sum(1 for x in positional_ids if x)
    total = len(op_names)

    if success_count == 0:
        # No op produced a clip — surface the first error verbatim.
        # When all errors are "timeout_waiting_video" this matches the
        # legacy single-op timeout contract; tests rely on it.
        first_err = next(iter(op_errors.values()), "timeout_waiting_video")
        return (
            {
                "raw_dispatch": dispatch,
                "last_poll": last_poll,
                "operation_names": op_names,
                "done": done_by_name,
                "op_errors": op_errors,
            },
            first_err,
        )

    # ≥1 op succeeded — ingest only the bytes we actually have.
    entries_with_urls = [
        e for e in succeeded_entries if isinstance(e, dict) and e.get("url")
    ]
    if entries_with_urls:
        try:
            media_service.ingest_urls(entries_with_urls)
        except Exception:  # noqa: BLE001
            logger.exception("auto-ingest from gen_video response failed")
    # Workflow-mode (Low Priority) deliveries arrive inline as base64 MP4
    # bytes on the `/v1/media/<id>` poll — there is no GCS URL to chase.
    # Plant the bytes in the local cache directly so the `/media/<id>` route
    # serves them like any URL-backed asset.
    for entry in succeeded_entries:
        if not isinstance(entry, dict):
            continue
        encoded = entry.get("encoded_video")
        mid = entry.get("media_id")
        if not isinstance(encoded, str) or not isinstance(mid, str):
            continue
        try:
            import base64 as _b64
            media_service.ingest_inline_bytes(
                mid, _b64.b64decode(encoded, validate=False),
                kind="video", mime="video/mp4",
            )
        except Exception:  # noqa: BLE001
            logger.exception("inline ingest from workflow-mode poll failed for %s", mid)

    partial_error: Optional[str] = None
    if op_errors:
        # De-dup distinct error codes for a compact one-line summary
        # (e.g. "1/4 variants blocked: PUBLIC_ERROR_UNSAFE_GENERATION").
        unique_errs = sorted({err for err in op_errors.values()})
        partial_error = (
            f"{len(op_errors)}/{total} variants blocked: {', '.join(unique_errs)}"
        )

    return (
        {
            "raw_dispatch": dispatch,
            "last_poll": last_poll,
            "operation_names": op_names,
            "media_ids": positional_ids,
            "media_entries": succeeded_entries,
            "op_errors": op_errors,
            "slot_errors": slot_errors,
            "partial_error": partial_error,
        },
        None,
    )


async def _handle_edit_image(params: dict) -> tuple[dict, Optional[str]]:
    from flowboard.services.flow_sdk import is_valid_project_id

    prompt = params.get("prompt")
    project_id = params.get("project_id")
    source_media_id = params.get("source_media_id") or params.get("sourceMediaId")
    if not isinstance(prompt, str) or not prompt.strip():
        return {}, "missing_prompt"
    if not isinstance(project_id, str) or not project_id.strip():
        return {}, "missing_project_id"
    project_id = project_id.strip()
    if not is_valid_project_id(project_id):
        return {}, "invalid_project_id"
    if not isinstance(source_media_id, str) or not source_media_id.strip():
        return {}, "missing_source_media_id"
    aspect = params.get("aspect_ratio") or "IMAGE_ASPECT_RATIO_LANDSCAPE"
    # Tier resolution — see _resolve_paygate_tier for rationale. Fail loud,
    # no silent fallback to Pro.
    tier = await _resolve_paygate_tier(params)
    if not tier:
        return {}, "paygate_tier_unknown"
    raw_refs = params.get("ref_media_ids")
    ref_ids: Optional[list[str]] = None
    if isinstance(raw_refs, list):
        cleaned = [m for m in raw_refs if isinstance(m, str) and m]
        ref_ids = cleaned or None
    image_model = params.get("image_model")
    if not isinstance(image_model, str) or not image_model.strip():
        image_model = None
    raw_count = params.get("variant_count")
    variant_count = 1
    if isinstance(raw_count, int) and raw_count > 0:
        variant_count = raw_count

    resp = await get_flow_sdk().edit_image(
        prompt=prompt.strip(),
        project_id=project_id,
        source_media_id=source_media_id.strip(),
        ref_media_ids=ref_ids,
        aspect_ratio=aspect,
        paygate_tier=tier,
        image_model=image_model,
        variant_count=variant_count,
    )
    if resp.get("error"):
        return resp, str(resp["error"])[:200]
    entries_with_urls = [
        e for e in (resp.get("media_entries") or []) if isinstance(e, dict) and e.get("url")
    ]
    if entries_with_urls:
        try:
            media_service.ingest_urls(entries_with_urls)
        except Exception:  # noqa: BLE001
            logger.exception("auto-ingest from edit_image response failed")
    
    # Extract media_ids from media_entries so the processor's node update
    # logic (lines 803-811) can populate node.data.mediaId/mediaIds correctly.
    # This matches the response structure from _handle_gen_image and _handle_gen_video.
    media_ids = [
        e.get("media_id") for e in (resp.get("media_entries") or [])
        if isinstance(e, dict) and isinstance(e.get("media_id"), str)
    ]
    if media_ids:
        resp["media_ids"] = media_ids
    
    return resp, None


def _trigger_downstream_videos(source_node_id: int, start_media_id: str) -> None:
    """Auto-trigger video generation for downstream Video nodes after image completion.
    
    When an image node completes successfully, this function finds all connected
    downstream Video nodes (with prompts) and automatically enqueues video
    generation requests using the newly generated image as the start frame.
    
    Args:
        source_node_id: The database ID of the completed image node
        start_media_id: The mediaId from the completed image to use as video start frame
    """
    try:
        from flowboard.db.models import Edge, Node, BoardFlowProject
        from sqlmodel import select
        
        with get_session() as s:
            # Find all downstream edges from this image node
            edges = s.exec(
                select(Edge).where(Edge.source_id == source_node_id)
            ).all()
            
            if not edges:
                return
            
            # Get the source node to retrieve board_id and project_id
            source_node = s.get(Node, source_node_id)
            if not source_node:
                return
            
            board_id = source_node.board_id
            
            # Get project_id for this board
            board_project = s.get(BoardFlowProject, board_id)
            if not board_project or not board_project.flow_project_id:
                logger.warning(
                    "auto-trigger: board %s has no project_id, skipping downstream videos",
                    board_id
                )
                return
            
            project_id = board_project.flow_project_id
            
            # Get paygate tier from flow_client (will be used for all triggered videos)
            from flowboard.services.flow_client import flow_client
            tier = flow_client.paygate_tier
            if tier is None:
                logger.warning("auto-trigger: paygate_tier unknown, skipping downstream videos")
                return
            
            # Process each downstream node
            for edge in edges:
                # `fallback-start-image` edges only supply a fallback start
                # frame to a clip; they must NOT themselves trigger generation
                # (that clip is driven by its own main upstream edge).
                if edge.target_handle == "fallback-start-image":
                    continue

                target_node = s.get(Node, edge.target_id)
                if not target_node:
                    continue

                # Only auto-trigger Video nodes
                if target_node.type != "video":
                    continue
                
                # Skip if node doesn't have a prompt
                node_data = target_node.data or {}
                prompt = node_data.get("prompt")
                if not isinstance(prompt, str) or not prompt.strip():
                    logger.info(
                        "auto-trigger: skipping video node %s (no prompt)",
                        target_node.short_id
                    )
                    continue
                
                # Skip if node is already running/queued/done
                if target_node.status in ("running", "queued", "done"):
                    logger.info(
                        "auto-trigger: skipping video node %s (status=%s)",
                        target_node.short_id, target_node.status
                    )
                    continue
                
                # Create and enqueue video generation request
                req = Request(
                    node_id=target_node.id,
                    type="gen_video",
                    params={
                        "prompt": prompt.strip(),
                        "project_id": project_id,
                        "start_media_id": start_media_id,
                        "aspect_ratio": node_data.get("aspectRatio") or "VIDEO_ASPECT_RATIO_LANDSCAPE",
                        "paygate_tier": tier,
                        "video_quality": node_data.get("videoQuality"),
                    },
                    status="queued",
                )
                s.add(req)
                s.flush()
                assert req.id is not None
                
                # Update target node status to queued
                target_node.status = "queued"
                s.add(target_node)
                
                logger.info(
                    "auto-trigger: enqueued video generation for node %s (req_id=%s)",
                    target_node.short_id, req.id
                )
            
            s.commit()
            
            # Enqueue all created requests to the worker
            with get_session() as s2:
                recent_reqs = s2.exec(
                    select(Request).where(
                        Request.node_id.in_([e.target_id for e in edges]),  # type: ignore[attr-defined]
                        Request.status == "queued",
                        Request.type == "gen_video",
                    )
                ).all()
                
                for req in recent_reqs:
                    if req.id is not None:
                        get_worker().enqueue(req.id)
                        
    except Exception as exc:  # noqa: BLE001
        logger.exception("auto-trigger: failed for node %s: %s", source_node_id, exc)


# ── Omni Flash r2v ────────────────────────────────────────────────────────
# Variable-duration video model with a distinct endpoint + body shape from
# Veo i2v. See agent/flowboard/services/flow_sdk.py::gen_video_omni for the
# request assembly. Single operation per request (no multi-source batching
# like Veo's start_media_ids), so the polling logic collapses to a single
# op + first-error-wins, simpler than _handle_gen_video.

async def _apply_narration_to_video(video_bytes: bytes, narration: str, duration_s: int) -> bytes:
    import tempfile
    import os
    from flowboard.services.tts import generate_speech
    
    with tempfile.TemporaryDirectory() as td:
        video_path = os.path.join(td, "video.mp4")
        audio_path = os.path.join(td, "audio.wav")
        out_path = os.path.join(td, "out.mp4")
        
        with open(video_path, "wb") as f:
            f.write(video_bytes)
            
        await generate_speech(narration, audio_path)
        
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", audio_path,
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-shortest",
            out_path
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error("ffmpeg failed to mux narration: %s", stderr.decode())
            return video_bytes
            
        with open(out_path, "rb") as f:
            return f.read()


async def _handle_gen_video_omni(params: dict) -> tuple[dict, Optional[str]]:
    from flowboard.services.flow_sdk import is_valid_project_id
    from flowboard.services.media_project_sync import (
        MediaSyncError,
        ensure_media_ids_in_project,
    )

    prompt = params.get("prompt")
    project_id = params.get("project_id")
    raw_refs = params.get("ref_media_ids")
    if not isinstance(raw_refs, list):
        # Also accept the legacy single-source field for symmetry with
        # Veo's start_media_id, so the same upstream-walk on the frontend
        # works without a special-case.
        raw_refs = (
            [params.get("start_media_id")]
            if isinstance(params.get("start_media_id"), str)
            else []
        )
    ref_media_ids = [m for m in raw_refs if isinstance(m, str) and m.strip()]
    duration_s = params.get("duration_s")

    if not isinstance(prompt, str) or not prompt.strip():
        return {}, "missing_prompt"
    if not isinstance(project_id, str) or not project_id.strip():
        return {}, "missing_project_id"
    project_id = project_id.strip()
    if not is_valid_project_id(project_id):
        return {}, "invalid_project_id"
    if not ref_media_ids:
        return {}, "missing_ref_media_ids"
    if not isinstance(duration_s, int) or duration_s not in (4, 6, 8, 10):
        return {}, "invalid_duration_s"
    aspect = params.get("aspect_ratio") or "VIDEO_ASPECT_RATIO_PORTRAIT"
    tier = params.get("paygate_tier") or flow_client.paygate_tier
    if tier is None:
        return {}, "paygate_tier_unknown"

    # ── Cross-project ref sync ────────────────────────────────────────
    # Flow scopes mediaIds to the project they were uploaded in. When
    # the user references media generated under another board's project
    # (the cross-board Reference library case), Flow returns 404 because
    # the asset is unknown in this project. Re-upload bytes from the
    # local cache and substitute the project-local id before dispatch.
    # First sync hits the Flow upload endpoint per ref; subsequent
    # syncs use the MediaProjectMapping cache and are free.
    try:
        synced_refs, sync_failures = await ensure_media_ids_in_project(
            ref_media_ids, project_id
        )
    except MediaSyncError as exc:
        return {}, f"sync_failed: {exc}"[:200]
    if not synced_refs:
        # Every ref failed to sync — surface the first reason.
        first = sync_failures[0][1] if sync_failures else "no_refs_synced"
        return (
            {"sync_failures": sync_failures},
            f"sync_failed: {first}"[:200],
        )
    if sync_failures:
        # Partial sync — log; proceed with the refs that worked.
        logger.warning(
            "gen_video_omni: %d ref(s) failed to sync, proceeding with %d",
            len(sync_failures), len(synced_refs),
        )

    sdk = get_flow_sdk()
    dispatch = await sdk.gen_video_omni(
        prompt=prompt.strip(),
        project_id=project_id,
        ref_media_ids=synced_refs,
        duration_s=duration_s,
        aspect_ratio=aspect,
        paygate_tier=tier,
    )
    if dispatch.get("error"):
        return dispatch, str(dispatch["error"])[:200]

    op_names = dispatch.get("operation_names") or []
    if not op_names:
        return dispatch, "no_operations_returned"
    workflows = dispatch.get("workflows") or None

    poll_attempts = 0
    last_poll: dict = {}
    done_by_name: dict[str, bool] = {name: False for name in op_names}
    entry_by_name: dict[str, dict] = {}
    op_errors: dict[str, str] = {}
    rid = params.get("__request_id")

    while (
        poll_attempts < VIDEO_POLL_MAX_CYCLES
        and not all(done_by_name.values())
    ):
        await asyncio.sleep(VIDEO_POLL_INTERVAL_S)
        poll_attempts += 1
        if _is_request_canceled(rid):
            return (
                {
                    "raw_dispatch": dispatch,
                    "last_poll": last_poll,
                    "operation_names": op_names,
                    "done": done_by_name,
                    "canceled": True,
                },
                "canceled",
            )
        last_poll = await sdk.check_async(op_names, workflows=workflows)
        if last_poll.get("error"):
            continue
        for op in last_poll.get("operations") or []:
            if not isinstance(op, dict):
                continue
            name = op.get("name")
            if not isinstance(name, str) or done_by_name.get(name, False):
                continue
            err = op.get("error")
            if isinstance(err, str) and err:
                done_by_name[name] = True
                op_errors[name] = err
                continue
            if op.get("done"):
                done_by_name[name] = True
                for e in op.get("media_entries") or []:
                    if isinstance(e, dict) and e.get("media_id"):
                        entry_by_name[name] = e
                        break

    for name in op_names:
        if not done_by_name.get(name) and name not in op_errors:
            op_errors[name] = "timeout_waiting_video"

    positional_ids: list[Optional[str]] = []
    slot_errors: list[Optional[str]] = []
    succeeded_entries: list[dict] = []
    for name in op_names:
        e = entry_by_name.get(name)
        if isinstance(e, dict) and isinstance(e.get("media_id"), str):
            positional_ids.append(e["media_id"])
            succeeded_entries.append(e)
            slot_errors.append(None)
        else:
            positional_ids.append(None)
            slot_errors.append(op_errors.get(name))

    if not any(positional_ids):
        first_err = next(iter(op_errors.values()), "timeout_waiting_video")
        return (
            {
                "raw_dispatch": dispatch,
                "last_poll": last_poll,
                "operation_names": op_names,
                "done": done_by_name,
                "op_errors": op_errors,
            },
            first_err,
        )

    entries_with_urls = [
        e for e in succeeded_entries if isinstance(e, dict) and e.get("url")
    ]
    if entries_with_urls:
        try:
            media_service.ingest_urls(entries_with_urls)
        except Exception:  # noqa: BLE001
            logger.exception("auto-ingest from gen_video_omni response failed")
    # Omni Flash uses workflow-mode polling: Flow delivers the rendered MP4
    # inline as base64 on `/v1/media/<id>` with no signed GCS URL. Plant the
    # bytes in the local cache so `/media/<id>` can serve them.
    narration = params.get("narration")

    for entry in succeeded_entries:
        if not isinstance(entry, dict):
            continue
        encoded = entry.get("encoded_video")
        mid = entry.get("media_id")
        if not isinstance(encoded, str) or not isinstance(mid, str):
            continue
        try:
            import base64 as _b64
            video_bytes = _b64.b64decode(encoded, validate=False)
            
            if narration and isinstance(narration, str) and narration.strip():
                try:
                    video_bytes = await _apply_narration_to_video(video_bytes, narration.strip(), duration_s)
                except Exception as e:
                    logger.exception("Failed to apply narration to video %s: %s", mid, e)

            media_service.ingest_inline_bytes(
                mid, video_bytes,
                kind="video", mime="video/mp4",
            )
        except Exception:  # noqa: BLE001
            logger.exception("inline ingest from omni workflow poll failed for %s", mid)

    return (
        {
            "raw_dispatch": dispatch,
            "last_poll": last_poll,
            "operation_names": op_names,
            "media_ids": positional_ids,
            "media_entries": succeeded_entries,
            "op_errors": op_errors,
            "slot_errors": slot_errors,
            "duration_s": duration_s,
        },
        None,
    )


async def _handle_upscale_video(params: dict) -> tuple[dict, Optional[str]]:
    project_id = params.get("project_id")
    media_id = params.get("media_id")
    if not isinstance(project_id, str) or not project_id.strip():
        return {}, "missing_project_id"
    if not isinstance(media_id, str) or not media_id.strip():
        return {}, "missing_media_id"

    tier = params.get("paygate_tier") or flow_client.paygate_tier
    if tier is None:
        return {}, "paygate_tier_unknown"

    aspect = params.get("aspect_ratio") or "VIDEO_ASPECT_RATIO_LANDSCAPE"
    resolution = params.get("resolution") or "VIDEO_RESOLUTION_4K"

    sdk = get_flow_sdk()
    dispatch = await sdk.upscale_video(
        media_id=media_id.strip(),
        project_id=project_id.strip(),
        paygate_tier=tier,
        aspect_ratio=aspect,
        resolution=resolution,
    )
    if dispatch.get("error"):
        return dispatch, str(dispatch["error"])[:200]

    op_names = dispatch.get("operation_names") or []
    if not op_names:
        return dispatch, "no_operations_returned"

    workflows = dispatch.get("workflows") or None

    poll_attempts = 0
    last_poll: dict = {}
    done_by_name: dict[str, bool] = {name: False for name in op_names}
    entry_by_name: dict[str, dict] = {}
    op_errors: dict[str, str] = {}
    rid = params.get("__request_id")

    while (
        poll_attempts < VIDEO_POLL_MAX_CYCLES
        and not all(done_by_name.values())
    ):
        await asyncio.sleep(VIDEO_POLL_INTERVAL_S)
        poll_attempts += 1
        if _is_request_canceled(rid):
            return (
                {
                    "raw_dispatch": dispatch,
                    "last_poll": last_poll,
                    "operation_names": op_names,
                    "done": done_by_name,
                    "canceled": True,
                },
                "canceled",
            )
        last_poll = await sdk.check_async(op_names, workflows=workflows)
        if last_poll.get("error"):
            continue
        for op in last_poll.get("operations") or []:
            if not isinstance(op, dict):
                continue
            name = op.get("name")
            if not isinstance(name, str) or done_by_name.get(name, False):
                continue
            err = op.get("error")
            if isinstance(err, str) and err:
                done_by_name[name] = True
                op_errors[name] = err
                continue
            if op.get("done"):
                done_by_name[name] = True
                for e in op.get("media_entries") or []:
                    if isinstance(e, dict) and e.get("media_id"):
                        entry_by_name[name] = e
                        break

    for name in op_names:
        if not done_by_name.get(name) and name not in op_errors:
            op_errors[name] = "timeout_waiting_video"

    positional_ids: list[Optional[str]] = []
    slot_errors: list[Optional[str]] = []
    succeeded_entries: list[dict] = []
    for name in op_names:
        e = entry_by_name.get(name)
        if isinstance(e, dict) and isinstance(e.get("media_id"), str):
            positional_ids.append(e["media_id"])
            succeeded_entries.append(e)
            slot_errors.append(None)
        else:
            positional_ids.append(None)
            slot_errors.append(op_errors.get(name))

    success_count = sum(1 for x in positional_ids if x)
    if success_count == 0:
        first_err = next(iter(op_errors.values()), "timeout_waiting_video")
        return (
            {
                "raw_dispatch": dispatch,
                "last_poll": last_poll,
                "operation_names": op_names,
                "done": done_by_name,
                "op_errors": op_errors,
            },
            first_err,
        )

    entries_with_urls = [
        e for e in succeeded_entries if isinstance(e, dict) and e.get("url")
    ]
    if entries_with_urls:
        try:
            media_service.ingest_urls(entries_with_urls)
        except Exception:  # noqa: BLE001
            logger.exception("auto-ingest from upscale_video response failed")

    for entry in succeeded_entries:
        if not isinstance(entry, dict):
            continue
        encoded = entry.get("encoded_video")
        mid = entry.get("media_id")
        if not isinstance(encoded, str) or not isinstance(mid, str):
            continue
        try:
            import base64 as _b64
            media_service.ingest_inline_bytes(
                mid, _b64.b64decode(encoded, validate=False),
                kind="video", mime="video/mp4",
            )
        except Exception:  # noqa: BLE001
            logger.exception("inline ingest from workflow-mode poll failed for %s", mid)

    return (
        {
            "raw_dispatch": dispatch,
            "last_poll": last_poll,
            "operation_names": op_names,
            "media_ids": positional_ids,
            "media_entries": succeeded_entries,
            "op_errors": op_errors,
            "slot_errors": slot_errors,
        },
        None,
    )


_DEFAULT_HANDLERS: dict[str, Handler] = {
    "proxy": _handle_proxy,
    "create_project": _handle_create_project,
    "gen_image": _handle_gen_image,
    "gen_video": _handle_gen_video,
    "gen_video_omni": _handle_gen_video_omni,
    "edit_image": _handle_edit_image,
    "upscale_video": _handle_upscale_video,
}


class WorkerController:
    """Single-consumer async queue worker."""

    def __init__(self, handlers: Optional[dict[str, Handler]] = None) -> None:
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._handlers = dict(handlers or _DEFAULT_HANDLERS)
        self._shutdown = asyncio.Event()
        self._active = 0
        self._started_at: Optional[float] = None

    # ── enqueue ────────────────────────────────────────────────────────────
    def enqueue(self, request_id: int) -> None:
        self._queue.put_nowait(request_id)

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def start(self) -> None:
        self._started_at = time.time()
        logger.info("worker started")
        while not self._shutdown.is_set():
            try:
                rid = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            await self._process_one(rid)

    def request_shutdown(self) -> None:
        self._shutdown.set()

    async def drain(self) -> None:
        # Wait for any in-flight task to finish.
        while self._active > 0:
            await asyncio.sleep(0.05)

    @property
    def active_count(self) -> int:
        return self._active

    @property
    def uptime_s(self) -> Optional[float]:
        if self._started_at is None:
            return None
        return time.time() - self._started_at

    # ── execution ──────────────────────────────────────────────────────────
    async def _process_one(self, rid: int) -> None:
        self._active += 1
        try:
            with get_session() as s:
                req = s.get(Request, rid)
                if req is None:
                    logger.warning("worker: request %s not found", rid)
                    return
                # Drift guard — the row might have been canceled (or
                # otherwise transitioned out of queued) between enqueue
                # and pop. The cancel endpoint mutates the DB row only;
                # it can't yank the rid back off the in-memory queue, so
                # we re-check here and bail without flipping status.
                if req.status != "queued":
                    logger.info(
                        "worker: skipping rid=%s (status=%s)", rid, req.status
                    )
                    return
                handler = self._handlers.get(req.type)
                if handler is None:
                    req.status = "failed"
                    req.error = f"unknown_request_type:{req.type}"
                    req.finished_at = datetime.now(timezone.utc)
                    s.add(req)
                    s.commit()
                    return

                req.status = "running"
                s.add(req)
                if req.node_id is not None:
                    from flowboard.db.models import Node
                    node = s.get(Node, req.node_id)
                    if node:
                        node.status = "running"
                        s.add(node)
                s.commit()
                node_id = req.node_id
                req_type = req.type
                params = dict(req.params or {})
                # Enrich with the request's node_id so handlers that need
                # to look up Node.data don't depend on the caller copying
                # it into params explicitly. Underscore prefix avoids
                # colliding with handler-defined fields.
                if req.node_id is not None and "__node_id" not in params:
                    params["__node_id"] = req.node_id
                # Long-running handlers re-check this rid between polls
                # to honor user-initiated cancels.
                params["__request_id"] = rid

            # Release the session during the possibly-long RPC.
            result, err = await handler(params)

            if not err and node_id is not None and req_type in ("gen_image", "gen_video", "gen_video_omni", "edit_image", "upscale_video"):
                try:
                    if not params.get("ai_face_transfer"):
                        apply_face_swap_to_node_media(node_id, req_type, result, params)
                except Exception as fs_err:
                    logger.error(f"Face swap post-processing failed: {fs_err}")

            with get_session() as s:
                req = s.get(Request, rid)
                if req is None:
                    return
                # Don't overwrite a canceled row with a late-arriving
                # done/failed stamp. The cancel endpoint already set
                # status='canceled' and finished_at; we only persist the
                # partial result for debugging visibility.
                if req.status == "canceled":
                    if isinstance(result, dict):
                        req.result = result
                        s.add(req)
                        s.commit()
                    return
                req.result = result if isinstance(result, dict) else {"value": result}
                req.finished_at = datetime.now(timezone.utc)
                if err:
                    # Video-poll exhaustion gets its own status so the UI
                    # can render "TIMEOUT" instead of a generic failure.
                    req.status = "timeout" if err == "timeout_waiting_video" else "failed"
                    req.error = err
                else:
                    req.status = "done"
                    req.error = None
                s.add(req)
                
                # Also update Node status directly in DB so it doesn't get stuck on browser refresh / page restart
                if node_id is not None:
                    from flowboard.db.models import Node
                    node = s.get(Node, node_id)
                    if node:
                        if err:
                            node.status = "timeout" if err == "timeout_waiting_video" else "error"
                            node_data = dict(node.data or {})
                            node_data["error"] = err
                            node.data = node_data
                        else:
                            node.status = "done"
                            if req_type in ("gen_image", "gen_video", "gen_video_omni", "edit_image", "upscale_video"):
                                res = result if isinstance(result, dict) else {}
                                media_ids = res.get("media_ids") or []
                                media_id = next((m for m in media_ids if m), None)
                                
                                node_data = dict(node.data or {})
                                node_data["mediaId"] = media_id
                                node_data["mediaIds"] = media_ids
                                node_data["renderedAt"] = datetime.now(timezone.utc).isoformat()
                                node_data.pop("error", None)
                                node.data = node_data
                        s.add(node)
                s.commit()
                
                # Automatic downstream triggering: after a successful image OR
                # video render, find downstream Video nodes and auto-trigger
                # their generation. Including `gen_video` lets a story chain
                # cascade (clip 1 → clip 2 → …) when clips are generated
                # individually — `_trigger_downstream_videos` uses this clip's
                # media as the next clip's start frame (its last frame is
                # extracted at dispatch) and already skips `fallback-start-image`
                # edges so only the real next clip fires.
                #
                # Skipped entirely for batch-created requests (`__from_batch`):
                # the batch's `run_batch_generation` drives the chain itself in
                # topological order, so auto-triggering here would double-enqueue
                # the next clip.
                if (
                    not err
                    and node_id is not None
                    and req_type in ("gen_image", "edit_image", "gen_video", "gen_video_omni")
                    and not params.get("__from_batch")
                ):
                    res = result if isinstance(result, dict) else {}
                    media_ids = res.get("media_ids") or []
                    primary_media_id = next((m for m in media_ids if m), None)
                    if primary_media_id:
                        _trigger_downstream_videos(node_id, primary_media_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("worker exception on rid=%s", rid)
            try:
                with get_session() as s:
                    req = s.get(Request, rid)
                    if req is not None and req.status != "canceled":
                        req.status = "failed"
                        err_str = str(exc).strip() or repr(exc)
                        req.error = err_str[:500]
                        req.finished_at = datetime.now(timezone.utc)
                        s.add(req)
                        
                        # Also update the Node status to error if exception occurs
                        if req.node_id is not None:
                            from flowboard.db.models import Node
                            node = s.get(Node, req.node_id)
                            if node:
                                node.status = "error"
                                node_data = dict(node.data or {})
                                node_data["error"] = err_str[:500]
                                node.data = node_data
                                s.add(node)
                        s.commit()
            except Exception:  # noqa: BLE001
                logger.exception("worker: failed to record failure for rid=%s", rid)
        finally:
            self._active -= 1


_worker: Optional[WorkerController] = None


def get_worker() -> WorkerController:
    global _worker
    if _worker is None:
        _worker = WorkerController()
    return _worker


def apply_face_swap_to_node_media(node_id: int, request_type: str, result: dict, params: Optional[dict] = None):
    """Post-process generated media with character face swap when explicit/upstream face refs exist."""
    import os
    from sqlmodel import select
    from flowboard.db.models import Node, Edge
    from flowboard.services import media as media_service
    from flowboard.services.face_swapper import swap_faces_in_image, swap_faces_in_video

    params = params or {}
    explicit_face_refs = params.get("face_ref_media_ids")
    face_ref_ids = [m for m in explicit_face_refs if isinstance(m, str) and m.strip()] if isinstance(explicit_face_refs, list) else []

    with get_session() as session:
        if not face_ref_ids:
            # Legacy fallback: direct upstream Character only. Do not fall back to
            # any random board Character; that caused unintended identity swaps.
            edges = session.exec(select(Edge).where(Edge.target_id == node_id)).all()
            for e in edges:
                src = session.get(Node, e.source_id)
                if src and src.type == "character":
                    char_media_id = (src.data or {}).get("mediaId")
                    if isinstance(char_media_id, str) and char_media_id.strip():
                        face_ref_ids.append(char_media_id.strip())
                    break

    if not face_ref_ids:
        return

    char_path = media_service.cached_path(face_ref_ids[0])
    if not char_path or not char_path.exists():
        return
    char_path_str = str(char_path)

    # Get the generated media ids from result
    media_ids = result.get("media_ids") or [result.get("media_id")]
    media_ids = [m for m in media_ids if m]
    
    for mid in media_ids:
        path = media_service.cached_path(mid)
        if not path or not path.exists():
            continue
            
        path_str = str(path)
        temp_out = f"{path_str}.swapped.mp4" if request_type.startswith("gen_video") else f"{path_str}.swapped.png"
        
        success = False
        if request_type.startswith("gen_video"):
            success = swap_faces_in_video(char_path_str, path_str, temp_out)
        else: # gen_image / edit_image
            success = swap_faces_in_image(char_path_str, path_str, temp_out)
            
        if success and os.path.exists(temp_out):
            try:
                # Safely overwrite in-place
                os.replace(temp_out, path_str)
                logger.info(f"Successfully face-swapped media {mid} for node {node_id}")
            except Exception as e:
                logger.error(f"Failed to overwrite swapped file: {e}")
                if os.path.exists(temp_out):
                    os.remove(temp_out)
