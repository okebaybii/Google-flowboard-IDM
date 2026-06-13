from pathlib import Path
from typing import Literal, Optional
import base64
import shutil

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import select

from flowboard.db import get_session
from flowboard.db.models import Asset, Board, Edge, Node, Request
from flowboard.short_id import generate_unique_short_id
from flowboard.services import media as media_service
from flowboard.services.flow_sdk import get_flow_sdk, is_valid_project_id

import logging

router = APIRouter(prefix="/api/nodes", tags=["nodes"])
logger = logging.getLogger(__name__)

NodeType = Literal[
    "character",
    "image",
    "video",
    "prompt",
    "note",
    "visual_asset",
    # Storyboard = thin image-node wrapper. Backend treats it the same as
    # `image` for storage / dispatch — see frontend/src/lib/storyboardPrompt.ts
    # for the template that drives gen_image.
    "Storyboard",
    "social_block",
    "video_assembly",
    "style_preset",
    "story_script",
]
NodeStatus = Literal["idle", "queued", "running", "done", "error"]

_COORD_MIN = -1_000_000.0
_COORD_MAX = 1_000_000.0
_SIZE_MAX = 100_000.0


class NodeCreate(BaseModel):
    board_id: int
    type: NodeType
    x: float = Field(default=0.0, ge=_COORD_MIN, le=_COORD_MAX)
    y: float = Field(default=0.0, ge=_COORD_MIN, le=_COORD_MAX)
    w: float = Field(default=240.0, gt=0, le=_SIZE_MAX)
    h: float = Field(default=160.0, gt=0, le=_SIZE_MAX)
    data: dict = {}
    status: NodeStatus = "idle"


class NodeUpdate(BaseModel):
    x: Optional[float] = Field(default=None, ge=_COORD_MIN, le=_COORD_MAX)
    y: Optional[float] = Field(default=None, ge=_COORD_MIN, le=_COORD_MAX)
    w: Optional[float] = Field(default=None, gt=0, le=_SIZE_MAX)
    h: Optional[float] = Field(default=None, gt=0, le=_SIZE_MAX)
    data: Optional[dict] = None
    status: Optional[NodeStatus] = None


@router.post("")
def create_node(body: NodeCreate):
    with get_session() as s:
        if not s.get(Board, body.board_id):
            raise HTTPException(404, "board not found")
        short_id = generate_unique_short_id(s, body.board_id)
        node = Node(
            board_id=body.board_id,
            short_id=short_id,
            type=body.type,
            x=body.x,
            y=body.y,
            w=body.w,
            h=body.h,
            data=body.data,
            status=body.status,
        )
        s.add(node)
        s.commit()
        s.refresh(node)
        return node


@router.patch("/{node_id}")
def update_node(node_id: int, body: NodeUpdate):
    """Partial update.

    The `data` field is **shallow-merged** into the existing JSON
    column rather than wholesale-replaced — earlier behavior dropped
    any sibling field the caller forgot to list, which silently erased
    `aspectRatio`, `aiBrief`, and other state every time the frontend
    sent a partial update. Merge is the natural REST PATCH semantic
    and prevents that whole class of regression.

    Merge depth is **one level** — patch keys at the top level of
    `data` are merged with existing keys, but if a key's value is
    itself a dict, the new dict REPLACES the old one (no recursive
    merge). All current FlowboardNodeData fields are scalars / arrays,
    so this matches the schema. If a future field needs nested-merge
    semantics, switch to a recursive walker here and update this
    docstring.

    Sentinel: a value of `null` in the data patch deletes the key. So
    callers that want to clear `aiBrief` after a regen pass
    `{aiBrief: null}` (still merge-safe — no risk of accidentally
    nuking unrelated fields). Missing keys are preserved.

    Non-`data` fields (`x`, `y`, `w`, `h`, `status`) keep the original
    setattr-replace semantic — no merge applied.
    """
    with get_session() as s:
        node = s.get(Node, node_id)
        if not node:
            raise HTTPException(404, "node not found")
        patch = body.model_dump(exclude_unset=True)
        for k, v in patch.items():
            if k == "data" and isinstance(v, dict):
                merged = dict(node.data or {})
                for dk, dv in v.items():
                    if dv is None:
                        merged.pop(dk, None)
                    else:
                        merged[dk] = dv
                node.data = merged
            else:
                setattr(node, k, v)
        s.add(node)
        s.commit()
        s.refresh(node)
        return node


@router.delete("/{node_id}")
def delete_node(node_id: int):
    """Delete a node + cascade.

    Edges are owned by the graph — delete them outright.
    Request + Asset rows are *historical* (activity feed, media cache)
    and have a nullable `node_id` FK. Detach them (set node_id=NULL)
    rather than delete, so:
      - the activity feed still shows the historical generation entries
      - saved References pointing at this node's media keep working
        (Asset row survives, and `/media/{id}` still resolves to the
        cached file on disk).

    Skipping this detach step caused a FOREIGN KEY constraint failure
    that aborted the whole transaction — the user saw the node vanish
    locally (optimistic via applyNodeChanges) but reload restored it
    because the backend never actually deleted it.
    """
    with get_session() as s:
        node = s.get(Node, node_id)
        if not node:
            raise HTTPException(404, "node not found")
        # Cascade delete associated SocialBlock and SocialBlockPost rows
        from flowboard.db.models import SocialBlock, SocialBlockPost
        block = s.exec(
            select(SocialBlock).where(SocialBlock.node_id == node_id)
        ).first()
        if block:
            posts = s.exec(
                select(SocialBlockPost).where(SocialBlockPost.social_block_id == block.id)
            ).all()
            for p in posts:
                s.delete(p)
            s.delete(block)
        # Detach historical children FIRST so the FK constraint is satisfied.
        orphan_requests = s.exec(
            select(Request).where(Request.node_id == node_id)
        ).all()
        for r in orphan_requests:
            r.node_id = None
            s.add(r)
        orphan_assets = s.exec(
            select(Asset).where(Asset.node_id == node_id)
        ).all()
        for a in orphan_assets:
            a.node_id = None
            s.add(a)
        # Edges go with the node.
        edges = s.exec(
            select(Edge).where((Edge.source_id == node_id) | (Edge.target_id == node_id))
        ).all()
        for e in edges:
            s.delete(e)
        s.delete(node)
        s.commit()
        return {
            "ok": True,
            "deleted_edges": [e.id for e in edges],
            "detached_requests": len(orphan_requests),
            "detached_assets": len(orphan_assets),
        }


class GenerateStoryRequest(BaseModel):
    prompt: Optional[str] = None
    sampleVideoUrl: Optional[str] = None
    projectId: Optional[str] = None


import json
from flowboard.services.llm import registry, secrets


def _image_aspect_ratio(width: int, height: int) -> str:
    if width <= 0 or height <= 0:
        return "IMAGE_ASPECT_RATIO_LANDSCAPE"
    ratio = width / height
    if 0.91 <= ratio <= 1.1:
        return "IMAGE_ASPECT_RATIO_SQUARE"
    if ratio > 1.1:
        return "IMAGE_ASPECT_RATIO_LANDSCAPE"
    return "IMAGE_ASPECT_RATIO_PORTRAIT"


def _scene_text(scene: dict, key: str) -> str:
    value = scene.get(key)
    return value.strip() if isinstance(value, str) else ""


def _build_continuity_video_prompt(
    *,
    scene_index: int,
    total_scenes: int,
    image_prompt: str,
    video_prompt: str,
    identity_prefix: str,
    has_sample_video: bool,
) -> str:
    total = max(total_scenes, 1)
    clip_no = scene_index + 1
    parts: list[str] = []

    if identity_prefix:
        parts.append(identity_prefix.strip())

    if scene_index == 0:
        parts.append(
            f"Continuous video sequence clip 1 of {total}. Use the connected "
            "start image as the opening frame. Keep the exact subject, face, "
            "outfit, background, lighting, lens, camera angle, and visual scale "
            "stable."
        )
        if image_prompt:
            parts.append(f"Opening frame context: {image_prompt}.")
    else:
        parts.append(
            f"Continuous video sequence clip {clip_no} of {total}. The first "
            f"frame must match the final frame of clip {clip_no - 1}: same pose, "
            "hand and foot placement, body orientation, facial expression, "
            "camera angle, background, lighting, outfit, and subject scale. "
            "Continue the motion from that exact pose without resetting the "
            "choreography, cutting to a new scene, or reintroducing the character."
        )
        if image_prompt:
            parts.append(
                "Use this only as the intended next-action context, not as a "
                f"new starting frame: {image_prompt}."
            )

    if has_sample_video:
        parts.append(
            "Follow the sample video's choreography timing, gesture rhythm, "
            "and camera rhythm across all clips as one continuous take."
        )

    if video_prompt:
        parts.append(f"Action beat for this clip: {video_prompt}.")
    elif image_prompt and scene_index == 0:
        parts.append("Action beat for this clip: subtle natural motion while holding continuity.")

    if scene_index < total - 1:
        parts.append(
            f"End clip {clip_no} on a clear hold pose that can continue directly "
            f"into clip {clip_no + 1}. Do not teleport, change identity, change "
            "clothes, change location, or hide the face."
        )
    else:
        parts.append(
            "This is the final clip: resolve the movement naturally and hold the "
            "final pose for a clean assembled ending."
        )

    return " ".join(part for part in parts if part).strip()


def _frame_face_score(cv2, frame) -> int:
    """Prefer a sample frame with a visible face; fallback caller handles zero."""
    try:
        cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
        detector = cv2.CascadeClassifier(str(cascade_path))
        if detector.empty():
            return 0
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = detector.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4)
        if faces is None or len(faces) == 0:
            return 0
        return max(int(w) * int(h) for (_x, _y, w, h) in faces)
    except Exception:
        return 0


async def _upload_sample_reference_frame(
    frame_path: str,
    *,
    project_id: Optional[str],
    story_node_id: int,
    width: int,
    height: int,
) -> Optional[dict]:
    if not project_id or not is_valid_project_id(project_id):
        logger.info("story sample reference upload skipped: missing/invalid project id")
        return None

    try:
        raw = Path(frame_path).read_bytes()
        image_b64 = base64.b64encode(raw).decode("ascii")
        resp = await get_flow_sdk().upload_image(
            image_base64=image_b64,
            mime_type="image/jpeg",
            project_id=project_id,
            file_name=f"story_sample_{story_node_id}.jpg",
        )
    except Exception as exc:
        logger.warning("story sample reference upload failed: %s", exc)
        return None

    if resp.get("error"):
        logger.warning("story sample reference upload failed: %s", resp.get("error"))
        return None
    media_id = resp.get("media_id")
    if not isinstance(media_id, str) or not media_service.is_valid_media_id(media_id):
        logger.warning("story sample reference upload returned invalid media id: %r", media_id)
        return None

    cache_path = media_service.MEDIA_CACHE_DIR / f"{media_id}.jpg"
    try:
        cache_path.write_bytes(raw)
    except OSError as exc:
        logger.warning("failed to cache story sample reference %s: %s", media_id, exc)
        return None

    with get_session() as s:
        row = s.exec(select(Asset).where(Asset.uuid_media_id == media_id)).first()
        if row is None:
            row = Asset(
                uuid_media_id=media_id,
                kind="image",
                local_path=str(cache_path),
                mime="image/jpeg",
            )
        else:
            row.kind = "image"
            row.local_path = str(cache_path)
            row.mime = "image/jpeg"
        s.add(row)
        s.commit()

    return {
        "media_id": media_id,
        "aspect_ratio": _image_aspect_ratio(width, height),
    }


@router.post("/story-script/{node_id}/generate")
async def generate_story_script(node_id: int, body: GenerateStoryRequest):
    """Segment a script/concept into multi-scene visual storyboard assets in database."""
    with get_session() as s:
        node = s.get(Node, node_id)
        if not node:
            raise HTTPException(404, "Node not found")
        if node.type != "story_script":
            raise HTTPException(400, "Node must be of type 'story_script'")

        board_id = node.board_id
        prompt_text = body.prompt or node.data.get("prompt", "")
        # A story can be driven by a text concept OR a reference video (whose
        # keyframes are extracted below and analysed by the LLM). Require at
        # least one — matching the frontend, which lets the user run with just
        # a sample video URL. Rejecting empty-prompt-but-has-video here was the
        # "Story prompt cannot be empty" bug for video-only runs.
        sample_video_url = body.sampleVideoUrl or node.data.get("sampleVideoUrl", "")
        has_prompt = bool(prompt_text and prompt_text.strip())
        has_video = bool(sample_video_url and str(sample_video_url).strip())
        if not has_prompt and not has_video:
            raise HTTPException(400, "Vui lòng nhập nội dung kịch bản hoặc link video mẫu.")

        # Get LLM provider
        saved_providers = secrets.read_active_providers()
        provider_name = saved_providers.get("auto_prompt") or saved_providers.get("planner") or "gemini"

        provider = registry.get_provider(provider_name)
        if provider is None or not await provider.is_available():
            raise HTTPException(503, f"Configured LLM provider '{provider_name}' is not available. Please verify Settings.")

        # Gather upstream reference text to enforce character/style consistency
        upstream_prompts = []
        incoming_edges = s.exec(select(Edge).where(Edge.target_id == node_id)).all()
        for e in incoming_edges:
            sn = s.get(Node, e.source_id)
            if sn and sn.type in ("image", "character", "prompt", "Storyboard", "style_preset"):
                p = sn.data.get("prompt", "").strip()
                if not p:
                    p = sn.data.get("aiBrief", "").strip()
                if p and p not in upstream_prompts:
                    upstream_prompts.append(p)

        reference_context = ""
        if upstream_prompts:
            reference_context = (
                "CRITICAL: The user has provided the following reference character/style descriptions:\n"
                + "\n".join(f"- {p}" for p in upstream_prompts) + "\n\n"
                "You MUST strictly incorporate and preserve these exact character descriptions, clothing, and visual styles in ALL your generated `image_prompt`s. Do not invent new character appearances.\n\n"
            )

    # Call the LLM provider
    system_prompt = (
        "You are a master cinematic filmmaker and AI video story writer. "
        "Analyze the provided short story or script concept and break it down into one continuous, highly descriptive, visual, multi-scene storyboard sequence (between 3 to 6 scenes depending on complexity). "
        "Treat every scene as the next connected motion beat of the same take, not as an independent reset. "
        "For each scene, you must provide:\n"
        "1. title: A concise scene title (in Vietnamese).\n"
        "2. image_prompt: A highly detailed, descriptive, visual image generation prompt describing the starting frame of the scene in English. Include subject, lighting, colors, background, and environment.\n"
        "3. video_prompt: A description of only the next motion beat, gesture transition, ending pose, and camera movement in English (e.g., 'she raises her right hand from shoulder height into a small wave, then lands in a stable front-facing pose'). Do not restart the scene, change identity, change location, or repeat the opening pose unless it is the first scene.\n"
        "4. narration: A Vietnamese voiceover narration text (max 2 sentences) that describes the storytelling or dialogue in this scene. Speak naturally in Vietnamese.\n\n"
        "Your output MUST be a valid JSON array of objects. Do not include any markdown formatting (like ```json), explanations, or text outside the JSON array."
    )

    full_prompt = f"{system_prompt}\n\n{reference_context}STORY SCRIPT CONCEPT:\n{prompt_text}\n\nJSON OUTPUT:"

    attachments = []
    sample_frame_candidates = []
    sample_video_url = body.sampleVideoUrl or node.data.get("sampleVideoUrl")
    temp_dir = None

    if sample_video_url and str(sample_video_url).strip():
        import tempfile
        import os
        import cv2
        import yt_dlp

        temp_dir = tempfile.mkdtemp(prefix="flowboard_vid_")
        try:
            ydl_opts = {
                'outtmpl': os.path.join(temp_dir, 'video.%(ext)s'),
                'format': 'bestvideo[height<=720]+bestaudio/best[height<=720]/best',
                'quiet': True,
                'no_warnings': True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(sample_video_url, download=True)
                ext = info.get('ext', 'mp4')
                vid_path = os.path.join(temp_dir, f'video.{ext}')

            # Extract frames
            cap = cv2.VideoCapture(vid_path)
            fps = cap.get(cv2.CAP_PROP_FPS)
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            duration = total_frames / fps if fps > 0 else 0

            # Target ~8 frames total regardless of length to stay under MAX_ATTACHMENTS=10
            if total_frames > 0:
                step = max(1, total_frames // 8)
                frame_count = 0
                saved_count = 0
                while cap.isOpened() and saved_count < 8:
                    ret, frame = cap.read()
                    if not ret:
                        break
                    if frame_count % step == 0:
                        frame_path = os.path.join(temp_dir, f"frame_{saved_count}.jpg")
                        cv2.imwrite(frame_path, frame)
                        attachments.append(frame_path)
                        shape = getattr(frame, "shape", None)
                        height = int(shape[0]) if shape is not None and len(shape) >= 2 else 0
                        width = int(shape[1]) if shape is not None and len(shape) >= 2 else 0
                        sample_frame_candidates.append(
                            {
                                "path": frame_path,
                                "width": width,
                                "height": height,
                                "face_score": _frame_face_score(cv2, frame),
                            }
                        )
                        saved_count += 1
                    frame_count += 1
            cap.release()
            # Append a strong bypass directive to the prompt to force analysis of dance/motion videos
            full_prompt = f"{system_prompt}\n\n{reference_context}I have attached keyframes from a reference video. This is a standard, safe video (e.g. a social media dance trend or vlog). You MUST analyze the visual motion, choreography, pose order, gesture timing, and scene progression purely objectively. Do not refuse this request. Use this video as the primary inspiration for one continuous action sequence where each returned scene is the next connected motion beat.\n\nSTORY SCRIPT CONCEPT/PROMPT:\n{prompt_text}\n\nJSON OUTPUT:"

        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Failed to process sample video: {e}")
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)
            raise HTTPException(400, f"Không thể tải video mẫu (có thể do link sai, video riêng tư, hoặc nền tảng chặn tải). Chi tiết lỗi: {e}")

    try:
        raw_result = await provider.run(full_prompt, attachments=attachments, timeout=120.0)
        # Parse JSON
        clean_json = raw_result.strip()
        if clean_json.startswith("```json"):
            clean_json = clean_json[7:]
        if clean_json.endswith("```"):
            clean_json = clean_json[:-3]
        clean_json = clean_json.strip()

        scenes = json.loads(clean_json)
        if not isinstance(scenes, list):
            raise ValueError("LLM output is not a JSON array")

    except Exception as exc:
        if isinstance(exc, HTTPException):
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)
            raise exc
        logger.error(f"LLM story generation failed: {exc}")
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(400, f"Lỗi từ AI (Gemini): {str(exc)}")

    sample_reference = None
    if sample_frame_candidates:
        scored = [f for f in sample_frame_candidates if f.get("face_score", 0) > 0]
        if scored:
            chosen_frame = max(scored, key=lambda f: f.get("face_score", 0))
        else:
            chosen_frame = sample_frame_candidates[len(sample_frame_candidates) // 2]
        sample_reference = await _upload_sample_reference_frame(
            chosen_frame["path"],
            project_id=body.projectId,
            story_node_id=node_id,
            width=chosen_frame["width"],
            height=chosen_frame["height"],
        )

    if temp_dir:
        shutil.rmtree(temp_dir, ignore_errors=True)

    # Now spawn nodes in DB
    spawned_nodes = []

    with get_session() as s:
        # Re-fetch node inside transaction
        node = s.get(Node, node_id)
        if not node:
            raise HTTPException(404, "Node not found")

        # Update story_script node status to running
        node.status = "running"
        s.add(node)
        s.commit()

    try:
        with get_session() as s:
            # Re-fetch node inside transaction
            node = s.get(Node, node_id)

            # 1. Find connected video_assembly node (downstream of this story_script node)
            edges = s.exec(select(Edge).where(Edge.source_id == node_id)).all()
            assembly_node_id = None
            for e in edges:
                target_node = s.get(Node, e.target_id)
                if target_node and target_node.type == "video_assembly":
                    assembly_node_id = target_node.id
                    break

            # If no assembly node connected, let's look for any assembly node on the same board
            if not assembly_node_id:
                assembly_node = s.exec(
                    select(Node).where(Node.board_id == board_id, Node.type == "video_assembly")
                ).first()
                if assembly_node:
                    assembly_node_id = assembly_node.id

            # Find all reference nodes connected upstream to the story_script node
            incoming_script_edges = s.exec(
                select(Edge).where(Edge.target_id == node_id)
            ).all()

            upstream_refs = []
            allowed_ref_types = {"character", "style_preset", "image", "visual_asset", "prompt", "Storyboard"}

            for se in incoming_script_edges:
                sn = s.get(Node, se.source_id)
                if sn and sn.type in allowed_ref_types:
                    if sn.id not in upstream_refs:
                        upstream_refs.append(sn.id)

            # Also search upstream of the video_assembly node if connected
            if assembly_node_id:
                incoming_assembly_edges = s.exec(
                    select(Edge).where(Edge.target_id == assembly_node_id)
                ).all()
                for se in incoming_assembly_edges:
                    sn = s.get(Node, se.source_id)
                    if sn and sn.type in allowed_ref_types:
                        if sn.id not in upstream_refs:
                            upstream_refs.append(sn.id)

            # Fallback 1: if no style preset is in refs, query all style presets on this board
            has_style = any(s.get(Node, rid).type == "style_preset" for rid in upstream_refs if s.get(Node, rid))
            if not has_style:
                board_presets = s.exec(
                    select(Node).where(Node.board_id == board_id, Node.type == "style_preset")
                ).all()
                for bp in board_presets:
                    if bp.id not in upstream_refs:
                        upstream_refs.append(bp.id)

            # Fallback 2: if no character is in refs, query all characters on this board
            has_char = any(s.get(Node, rid).type == "character" for rid in upstream_refs if s.get(Node, rid))
            if not has_char:
                board_characters = s.exec(
                    select(Node).where(Node.board_id == board_id, Node.type == "character")
                ).all()
                for bc in board_characters:
                    if bc.id not in upstream_refs:
                        upstream_refs.append(bc.id)

            base_x = node.x
            base_y = node.y

            def _node_type(ref_id: int) -> Optional[str]:
                ref_node = s.get(Node, ref_id)
                return ref_node.type if ref_node else None

            if sample_reference:
                sample_short_id = generate_unique_short_id(s, board_id)
                sample_ref_node = Node(
                    board_id=board_id,
                    short_id=sample_short_id,
                    type="visual_asset",
                    x=base_x + 320,
                    y=base_y - 320,
                    data={
                        "title": "Sample video reference",
                        "prompt": (
                            "Reference frame from the sample video. Preserve the same "
                            "on-screen subject identity, face, hairstyle, outfit, body "
                            "proportions, dance style, and visual setting."
                        ),
                        "aiBrief": (
                            "Primary identity and style reference extracted from the "
                            "sample video; use it to keep the generated subject consistent."
                        ),
                        "mediaId": sample_reference["media_id"],
                        "mediaIds": [sample_reference["media_id"]],
                        "variantCount": 1,
                        "aspectRatio": sample_reference["aspect_ratio"],
                        "sampleVideoUrl": sample_video_url,
                        "sourceStoryScriptId": node_id,
                    },
                    status="done",
                )
                s.add(sample_ref_node)
                s.flush()

                # Character nodes are the authoritative identity source.
                # Keep sample-video frames after characters so Flow sees them
                # as motion/style references instead of replacing the face.
                last_character_idx = max(
                    (
                        idx
                        for idx, ref_id in enumerate(upstream_refs)
                        if _node_type(ref_id) == "character"
                    ),
                    default=-1,
                )
                upstream_refs.insert(last_character_idx + 1, sample_ref_node.id)
                spawned_nodes.append(sample_ref_node)

                asset = s.exec(
                    select(Asset).where(Asset.uuid_media_id == sample_reference["media_id"])
                ).first()
                if asset and asset.node_id is None:
                    asset.node_id = sample_ref_node.id
                    s.add(asset)

            character_ref_ids = [
                ref_id for ref_id in upstream_refs if _node_type(ref_id) == "character"
            ]
            identity_prefix = ""
            if character_ref_ids:
                identity_prefix = (
                    "Use the connected Character reference as the authoritative "
                    "identity source: preserve the exact same face, facial "
                    "features, hairstyle, age, skin tone, and overall person. "
                    "The sample video reference, if present, is only for dance "
                    "motion, pose timing, camera rhythm, clothing/scene hints, "
                    "and choreography; do not copy or invent a different dancer. "
                    "Keep the face visible, recognizable, and not hidden by "
                    "silhouette, heavy backlight, blur, or extreme distance. "
                )
            elif sample_reference:
                identity_prefix = (
                    "Use the connected sample video reference as the authoritative "
                    "identity source: preserve the same face, hairstyle, outfit, body "
                    "proportions, dance-video visual style, and setting. Do not invent "
                    "a different character. "
                )

            prev_vid_node = None
            for i, scene in enumerate(scenes):
                image_prompt = _scene_text(scene, "image_prompt")
                video_prompt = _scene_text(scene, "video_prompt")

                # Only spawn the base image for the very first scene
                if i == 0:
                    first_image_prompt = image_prompt
                    if identity_prefix and identity_prefix not in first_image_prompt:
                        first_image_prompt = f"{identity_prefix}{first_image_prompt}"
                    img_short_id = generate_unique_short_id(s, board_id)
                    img_node = Node(
                        board_id=board_id,
                        short_id=img_short_id,
                        type="image",
                        x=base_x + 320,
                        y=base_y - 100,
                        data={
                            "title": scene.get("title", f"Cảnh {i+1} - Ảnh"),
                            "prompt": first_image_prompt,
                            "aspectRatio": "IMAGE_ASPECT_RATIO_LANDSCAPE"
                        },
                        status="idle"
                    )
                    s.add(img_node)
                    s.flush()

                    # Auto-connect all upstream reference nodes to the newly spawned image node
                    for ref_id in upstream_refs:
                        edge_ref = Edge(
                            board_id=board_id,
                            source_id=ref_id,
                            target_id=img_node.id,
                            kind="ref"
                        )
                        s.add(edge_ref)

                    spawned_nodes.append(img_node)
                    base_img_node = img_node

                combined_prompt = _build_continuity_video_prompt(
                    scene_index=i,
                    total_scenes=len(scenes),
                    image_prompt=image_prompt,
                    video_prompt=video_prompt,
                    identity_prefix=identity_prefix,
                    has_sample_video=bool(sample_video_url and str(sample_video_url).strip()),
                )

                # Spawn video node
                vid_short_id = generate_unique_short_id(s, board_id)
                vid_node = Node(
                    board_id=board_id,
                    short_id=vid_short_id,
                    type="video",
                    x=base_x + (i + 1) * 320,
                    y=base_y + 120,
                    data={
                        "title": scene.get("title", f"Cảnh {i+1} - Clip"),
                        "prompt": combined_prompt,
                        "narration": scene.get("narration", ""),
                        "aspectRatio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
                        "sourceImagePrompt": image_prompt,
                        "sourceVideoPrompt": video_prompt,
                        "sequenceIndex": i,
                        "sequenceTotal": len(scenes),
                        "continuityMode": "chain",
                        "requiresPreviousClip": i > 0,
                        "fallbackStartImage": i > 0,
                    },
                    status="idle"
                )
                s.add(vid_node)
                s.flush()

                # Chaining logic: first video connects to base_img_node, subsequent videos connect to the previous video
                source_id_for_video = prev_vid_node.id if prev_vid_node else base_img_node.id

                edge1 = Edge(
                    board_id=board_id,
                    source_id=source_id_for_video,
                    target_id=vid_node.id,
                    kind="ref"
                )
                s.add(edge1)

                if prev_vid_node is not None:
                    fallback_edge = Edge(
                        board_id=board_id,
                        source_id=base_img_node.id,
                        target_id=vid_node.id,
                        kind="ref",
                        target_handle="fallback-start-image",
                    )
                    s.add(fallback_edge)

                # Keep upstream references visible on every clip. Image/Omni
                # paths can pass these as media refs, and Veo prompts still use
                # the same locked character/context wording.
                for ref_id in upstream_refs:
                    edge_char_to_vid = Edge(
                        board_id=board_id,
                        source_id=ref_id,
                        target_id=vid_node.id,
                        kind="ref"
                    )
                    s.add(edge_char_to_vid)

                prev_vid_node = vid_node

                spawned_nodes.append(vid_node)

                # Connect video to assembly
                if assembly_node_id:
                    edge2 = Edge(
                        board_id=board_id,
                        source_id=vid_node.id,

                        target_id=assembly_node_id,
                        kind="ref"
                    )
                    s.add(edge2)

            # Update story_script node status to done and save prompt
            node.status = "done"
            node_data = {**dict(node.data), "prompt": prompt_text}
            if sample_reference:
                node_data["sampleVideoReferenceMediaId"] = sample_reference["media_id"]
            node.data = node_data
            s.add(node)
            s.commit()

            return {
                "ok": True,
                "scenes_count": len(scenes),
                "spawned_nodes": [n.id for n in spawned_nodes]
            }
    except Exception as e:
        with get_session() as s:
            node = s.get(Node, node_id)
            if node:
                node.status = "error"
                s.add(node)
                s.commit()
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(500, f"Error spawning storyboard nodes: {str(e)}")

