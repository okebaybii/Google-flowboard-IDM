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
    # `image` for storage / dispatch â€” see frontend/src/lib/storyboardPrompt.ts
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
    column rather than wholesale-replaced â€” earlier behavior dropped
    any sibling field the caller forgot to list, which silently erased
    `aspectRatio`, `aiBrief`, and other state every time the frontend
    sent a partial update. Merge is the natural REST PATCH semantic
    and prevents that whole class of regression.

    Merge depth is **one level** â€” patch keys at the top level of
    `data` are merged with existing keys, but if a key's value is
    itself a dict, the new dict REPLACES the old one (no recursive
    merge). All current FlowboardNodeData fields are scalars / arrays,
    so this matches the schema. If a future field needs nested-merge
    semantics, switch to a recursive walker here and update this
    docstring.

    Sentinel: a value of `null` in the data patch deletes the key. So
    callers that want to clear `aiBrief` after a regen pass
    `{aiBrief: null}` (still merge-safe â€” no risk of accidentally
    nuking unrelated fields). Missing keys are preserved.

    Non-`data` fields (`x`, `y`, `w`, `h`, `status`) keep the original
    setattr-replace semantic â€” no merge applied.
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

    Edges are owned by the graph â€” delete them outright.
    Request + Asset rows are *historical* (activity feed, media cache)
    and have a nullable `node_id` FK. Detach them (set node_id=NULL)
    rather than delete, so:
      - the activity feed still shows the historical generation entries
      - saved References pointing at this node's media keep working
        (Asset row survives, and `/media/{id}` still resolves to the
        cached file on disk).

    Skipping this detach step caused a FOREIGN KEY constraint failure
    that aborted the whole transaction â€” the user saw the node vanish
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


def _frame_sharpness(cv2, frame) -> float:
    """Subject-weighted focus measure (variance of the Laplacian).

    Motion-blurred / out-of-focus frames have low high-frequency energy and thus
    a low Laplacian variance, so this lets us pick the crispest frame in each
    segment instead of whatever frame lands on the stride. Like the sharpest-
    frame picker in services/media.py, we weight the center/upper-body region so
    a sharp wall doesn't beat a frame where the subject (and face) is crisp, and
    penalize blown-out / crushed exposures. Returns 0.0 if OpenCV lacks the ops
    (e.g. under the test's fake cv2)."""
    try:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape[:2]

        cx1, cx2 = int(w * 0.18), int(w * 0.82)
        cy1, cy2 = int(h * 0.12), int(h * 0.88)
        center = gray[cy1:cy2, cx1:cx2]
        upper = gray[int(h * 0.12):int(h * 0.58), int(w * 0.22):int(w * 0.78)]

        full_sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        center_sharpness = float(cv2.Laplacian(center, cv2.CV_64F).var()) if center.size else 0.0
        upper_sharpness = float(cv2.Laplacian(upper, cv2.CV_64F).var()) if upper.size else 0.0

        brightness = float(center.mean()) if center.size else float(gray.mean())
        exposure_penalty = 0.55 if brightness < 35 or brightness > 235 else 1.0

        return (
            center_sharpness * 0.55
            + upper_sharpness * 0.30
            + full_sharpness * 0.15
        ) * exposure_penalty
    except Exception:
        return 0.0


def _write_jpeg(cv2, path: str, frame) -> None:
    """Write a frame to JPEG at high quality so the reference stays crisp.

    Applies a gentle unsharp mask first (matching services/media.py) to recover
    soft video frames without altering layout. Falls back to plain writes when
    OpenCV ops / the JPEG-quality flag are unavailable (keeps the test's 2-arg
    fake `imwrite` working)."""
    try:
        blurred = cv2.GaussianBlur(frame, (0, 0), 1.0)
        frame = cv2.addWeighted(frame, 1.35, blurred, -0.35, 0)
    except Exception:
        pass

    quality_flag = getattr(cv2, "IMWRITE_JPEG_QUALITY", None)
    if quality_flag is not None:
        try:
            cv2.imwrite(path, frame, [int(quality_flag), 95])
            return
        except TypeError:
            pass
    cv2.imwrite(path, frame)


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
        node_data = node.data or {}
        prompt_text = (body.prompt if body.prompt is not None else node_data.get("prompt", "")) or ""
        sample_video_url = (body.sampleVideoUrl if body.sampleVideoUrl is not None else node_data.get("sampleVideoUrl", "")) or ""
        has_prompt = bool(prompt_text and prompt_text.strip())
        has_video = bool(sample_video_url and str(sample_video_url).strip())
        if not has_prompt:
            if has_video:
                prompt_text = (
                    "Analyze the provided sample video and split it into a continuous multi-scene storyboard. "
                    "Focus on subject, motion, rhythm, camera angle, background, and connected poses."
                )
            else:
                raise HTTPException(400, "Please enter a story prompt or sample video link.")

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
        "2. image_prompt: A highly detailed, descriptive, visual image generation prompt describing the starting frame of the scene in English. Include subject, lighting, colors, background, and environment. ALSO describe the EXACT framing and pose seen in the frame: the shot size / camera distance (e.g. full-body, knees-up, medium), whether the legs and lower body are in frame, the body pose, stance, hand/arm position, body angle, and the facial expression. If the frame is a full-body or knees-up shot, the prompt MUST say full-body / knees-up and keep the legs and full outfit in frame — never describe it as a tight upper-body or face close-up, and never add a smile or change the pose unless the source frame shows it.\n"
        "3. video_prompt: A description of only the next motion beat, gesture transition, ending pose, and camera movement in English (e.g., 'she raises her right hand from shoulder height into a small wave, then lands in a stable front-facing pose'). Do not restart the scene, change identity, change location, or repeat the opening pose unless it is the first scene.\n"
        "4. narration: A Vietnamese voiceover narration text (max 2 sentences) that describes the storytelling or dialogue in this scene. Speak naturally in Vietnamese.\n\n"
        "ATTRACTIVE / ALLURE STYLING — CONDITIONAL: When the main subject is a WOMAN and the scene is a casual / fashion / lifestyle / dance / beauty / social-media context, write each `image_prompt` so the figure looks striking, attractive and gợi cảm (sexy) — MORE flattering than a plain snapshot, while staying tasteful and platform-safe (no nudity, no explicit or vulgar framing). In that case the `image_prompt` MUST: keep the SAME revealing / fitted outfit from the source frame (e.g. crop top, off-shoulder top, denim shorts, fitted dress) — never cover it up or swap it for modest clothing; keep full-body or knees-up framing so the whole figure and legs read; and add figure-flattering description: well-proportioned hourglass / slim-curvy body, defined waist, long legs, gentle S-curve posture, weight on one hip, graceful confident posing, smooth flattering skin, and warm glamour lighting that sculpts the body. Do NOT apply this for male subjects, children, product-only shots, or formal / corporate / professional contexts — keep those neutral and natural.\n\n"
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
            # Grab the highest-resolution stream we can (up to 1080p) so the
            # extracted reference frames are sharp. Audio is irrelevant for frame
            # sampling, so prefer a video-only stream to avoid a needless mux.
            ydl_opts = {
                'outtmpl': os.path.join(temp_dir, 'video.%(ext)s'),
                'format': 'bestvideo[height<=1080]/best[height<=1080]/bestvideo/best',
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

            # Split the timeline into MAX_CANDIDATE_FRAMES even segments and, for
            # each, keep the SHARPEST frame (highest Laplacian variance) instead
            # of whatever frame lands on a fixed stride — fixed-stride sampling
            # routinely lands on motion-blurred frames. A visible face wins ties
            # so the chosen reference shows the subject clearly. The first 8
            # winners go to Gemini for motion analysis (attachment budget); the
            # full ordered list is later mapped onto the returned scenes so each
            # clip can start from the REAL frame at that point in the source
            # video (timeline-aligned). `frac` (0..1) records each frame's
            # position so we can pick the frame nearest a scene's timestamp.
            MAX_CANDIDATE_FRAMES = 12
            if total_frames > 0:
                # Bound decode cost on long/high-fps clips: scan at most
                # ~READ_BUDGET frames spread evenly across the whole video, which
                # still gives several candidates per segment to compare.
                READ_BUDGET = 240
                read_stride = max(1, total_frames // READ_BUDGET)
                # best[bucket] = (sharpness, face_score, frame_copy, frac)
                best: dict[int, tuple] = {}
                frame_count = 0
                while cap.isOpened():
                    ret, frame = cap.read()
                    if not ret:
                        break
                    if frame_count % read_stride == 0:
                        frac = frame_count / total_frames
                        bucket = min(MAX_CANDIDATE_FRAMES - 1, int(frac * MAX_CANDIDATE_FRAMES))
                        sharpness = _frame_sharpness(cv2, frame)
                        face_score = _frame_face_score(cv2, frame)
                        # Prefer a visible face, then the crispest frame.
                        rank = (1 if face_score > 0 else 0, sharpness)
                        prev = best.get(bucket)
                        if prev is None or rank > (1 if prev[1] > 0 else 0, prev[0]):
                            frame_copy = frame.copy() if hasattr(frame, "copy") else frame
                            best[bucket] = (sharpness, face_score, frame_copy, frac)
                    frame_count += 1

                for saved_count, bucket in enumerate(sorted(best.keys())):
                    sharpness, face_score, frame, frac = best[bucket]
                    frame_path = os.path.join(temp_dir, f"frame_{saved_count}.jpg")
                    _write_jpeg(cv2, frame_path, frame)
                    # Only the first 8 frames go to Gemini (attachment cap);
                    # the rest still serve as timeline anchors for scenes.
                    if len(attachments) < 8:
                        attachments.append(frame_path)
                    shape = getattr(frame, "shape", None)
                    height = int(shape[0]) if shape is not None and len(shape) >= 2 else 0
                    width = int(shape[1]) if shape is not None and len(shape) >= 2 else 0
                    sample_frame_candidates.append(
                        {
                            "path": frame_path,
                            "width": width,
                            "height": height,
                            "face_score": face_score,
                            "frac": frac,
                        }
                    )
            cap.release()
            # Analyze reference video with production/prompt-engineering structure,
            # then convert that analysis into the JSON scene format required by Flowboard.
            video_analysis_instruction = (
                "HÃ£y Ä‘Ã³ng vai má»™t chuyÃªn gia sáº£n xuáº¥t video vÃ  báº­c tháº§y viáº¿t prompt AI "
                "(dÃ nh cho Midjourney, Runway, hoáº·c Sora). Dá»±a vÃ o video tÃ´i cung cáº¥p, "
                "hÃ£y phÃ¢n tÃ­ch chi tiáº¿t tá»«ng khung hÃ¬nh vÃ  táº¡o prompt tiáº¿ng Anh Ä‘á»ƒ AI táº¡o "
                "sáº£n pháº©m tÆ°Æ¡ng tá»±.\n\n"
                "TrÆ°á»›c khi viáº¿t prompt cho tá»«ng scene, bÃ³c tÃ¡ch video theo cáº¥u trÃºc sau:\n"
                "- Chá»§ thá»ƒ chÃ­nh: mÃ´ táº£ chi tiáº¿t ngoáº¡i hÃ¬nh, trang phá»¥c, Ä‘áº·c Ä‘iá»ƒm ná»•i báº­t.\n"
                "- HÃ nh Ä‘á»™ng & Biá»ƒu cáº£m: tá»‘c Ä‘á»™ di chuyá»ƒn, ngÃ´n ngá»¯ cÆ¡ thá»ƒ.\n"
                "- Bá»‘i cáº£nh xung quanh: khÃ´ng gian, Ä‘á»“ váº­t phá»¥ trá»£, Ä‘á»™ sÃ¢u trÆ°á»ng áº£nh.\n"
                "- Ká»¹ thuáº­t quay: cá»¡ cáº£nh (Wide shot, Close-up, Medium shot), gÃ³c mÃ¡y "
                "(Low angle, High angle), chuyá»ƒn Ä‘á»™ng camera (Pan, Tilt, Tracking, Slow motion).\n"
                "- Ãnh sÃ¡ng & MÃ u sáº¯c: loáº¡i Ã¡nh sÃ¡ng (Cinematic lighting, Natural light, Neon) "
                "vÃ  báº£ng mÃ u chá»§ Ä‘áº¡o.\n"
                "- Cháº¥t lÆ°á»£ng & Phong cÃ¡ch: 8k, photorealistic, 35mm lens, highly detailed.\n\n"
                "Sau khi phÃ¢n tÃ­ch, tá»•ng há»£p thÃ nh prompt tiáº¿ng Anh liÃªn tá»¥c, ngÄƒn cÃ¡ch báº±ng "
                "dáº¥u pháº©y, tá»‘i Æ°u cho AI táº¡o video/hÃ¬nh áº£nh. Use those English prompts inside "
                "the JSON fields below."
            )
            full_prompt = (
                f"{system_prompt}\n\n{reference_context}"
                "I have attached keyframes from a reference video. This is a standard, safe video "
                "(e.g. a social media dance trend or vlog). You MUST analyze the visual motion, "
                "choreography, pose order, gesture timing, scene progression, background, props, "
                "camera, lighting, color palette, and production style objectively. Do not refuse.\n\n"
                f"{video_analysis_instruction}\n\n"
                "IMPORTANT OUTPUT RULE: Return ONLY the required JSON array. Do not include the "
                "written analysis outside JSON. Each `image_prompt` must be a highly detailed "
                "English comma-separated prompt preserving subject, outfit, background, props, "
                "camera, lighting, colors, depth of field, quality/style. Each `video_prompt` "
                "must describe the next motion beat, expression/body language, timing, and camera movement.\n\n"
                f"STORY SCRIPT CONCEPT/PROMPT:\n{prompt_text}\n\nJSON OUTPUT:"
            )

        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Failed to process sample video: {e}")
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)
            raise HTTPException(400, f"KhÃ´ng thá»ƒ táº£i video máº«u (cÃ³ thá»ƒ do link sai, video riÃªng tÆ°, hoáº·c ná»n táº£ng cháº·n táº£i). Chi tiáº¿t lá»—i: {e}")

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
        raise HTTPException(400, f"Lá»—i tá»« AI (Gemini): {str(exc)}")

    sample_reference = None
    # Per-scene timeline-aligned frames: scene_frame_refs[i] holds the uploaded
    # source frame nearest scene i's position in the video (or None). When
    # present, each scene's clip starts from the REAL source frame so the
    # result tracks the original's composition/progression, and the
    # auto face-swap (if a Character is connected) replaces the face.
    scene_frame_refs: list[Optional[dict]] = []
    if sample_frame_candidates:
        # Hero frame (best visible face) â€” kept as a global identity/style ref.
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

        # Map one source frame to each scene by normalized timeline position.
        # Scene i sits at (i + 0.5) / N of the way through the video; pick the
        # extracted frame whose `frac` is closest. Upload each once; reuse the
        # hero upload when a scene maps to the same frame to save round-trips.
        n_scenes = max(len(scenes), 1)
        uploaded_by_path: dict[str, Optional[dict]] = {}
        if sample_reference is not None:
            uploaded_by_path[chosen_frame["path"]] = sample_reference
        ordered = sorted(sample_frame_candidates, key=lambda f: f.get("frac", 0.0))
        for i in range(len(scenes)):
            target = (i + 0.5) / n_scenes
            nearest = min(ordered, key=lambda f: abs(f.get("frac", 0.0) - target))
            path = nearest["path"]
            if path not in uploaded_by_path:
                uploaded_by_path[path] = await _upload_sample_reference_frame(
                    nearest["path"],
                    project_id=body.projectId,
                    story_node_id=node_id,
                    width=nearest["width"],
                    height=nearest["height"],
                )
            scene_frame_refs.append(uploaded_by_path[path])

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

            has_character_ref = any(_node_type(ref_id) == "character" for ref_id in upstream_refs)

            if sample_reference:
                # Keep the uploaded hero sample reference only as story metadata.
                # Do not spawn a global visual_asset node on the canvas; per-scene
                # sample-frame refs below are the only sample refs used for images.
                pass

            character_ref_ids = [
                ref_id for ref_id in upstream_refs if _node_type(ref_id) == "character"
            ]
            identity_prefix = ""
            if character_ref_ids:
                identity_prefix = (
                    "Use the connected Character reference as the authoritative identity source. "
                    "REFERENCE PRIORITY: Character reference is face/identity ONLY. "
                    "Sample video reference is the authoritative background/scene/composition source. "
                    "Preserve the exact same Character face, facial features, hairstyle, age, "
                    "skin tone, and overall person. Use the sample video for location, background, "
                    "props, lighting, camera angle, framing, outfit/body pose, choreography, and "
                    "scene style. Replace only the sample dancer's face/identity with the Character; "
                    "never copy the sample dancer's face, and never change the sample background into "
                    "a different location unless the user's prompt explicitly requests it. Keep the "
                    "Character face visible, recognizable, front-readable, and not hidden by silhouette, "
                    "heavy backlight, blur, mask, hair, hands, or extreme distance. "
                )
            elif sample_reference:
                identity_prefix = (
                    "Use the connected sample video reference as the authoritative "
                    "identity source: preserve the same face, hairstyle, outfit, body "
                    "proportions, dance-video visual style, and setting. Do not invent "
                    "a different character. "
                )

            # When the user supplied a sample video, we anchor EACH scene to the
            # real source frame at that point in the timeline (scene_frame_refs).
            # Every scene then spawns its own "done" image node holding that
            # frame, and the scene's clip starts from it â€” so the generated video
            # tracks the original's composition/progression shot-by-shot, and the
            # auto face-swap (Character connected) puts your character's face on
            # top. Without a sample video we keep the classic behaviour: only the
            # first scene gets an AI-generated base image and clips chain off the
            # previous clip's last frame.
            use_source_frames = bool(scene_frame_refs)

            prev_vid_node = None
            base_img_node = None
            for i, scene in enumerate(scenes):
                image_prompt = _scene_text(scene, "image_prompt")
                video_prompt = _scene_text(scene, "video_prompt")

                scene_frame = scene_frame_refs[i] if i < len(scene_frame_refs) else None

                if use_source_frames and scene_frame:
                    # Per-scene sample frame reference. This is NOT the final image;
                    # it feeds the generated scene image together with Character refs.
                    frame_short_id = generate_unique_short_id(s, board_id)
                    frame_ref_node = Node(
                        board_id=board_id,
                        short_id=frame_short_id,
                        type="visual_asset",
                        x=base_x + 320,
                        y=base_y + i * 240,
                        data={
                            "title": scene.get("title", f"Scene {i+1} - Sample frame"),
                            "prompt": (
                                "Timeline sample frame reference. Preserve this frame's background, "
                                "composition, camera angle, lighting, outfit/body pose, and scene style. "
                                "Use Character refs only for face/identity."
                            ),
                            "aiBrief": "Per-scene sample frame used as background/pose/camera reference.",
                            "mediaId": scene_frame["media_id"],
                            "mediaIds": [scene_frame["media_id"]],
                            "variantCount": 1,
                            "aspectRatio": scene_frame.get("aspect_ratio") or "IMAGE_ASPECT_RATIO_PORTRAIT",
                            "sourceVideoFrame": True,
                            "sourceStoryScriptId": node_id,
                            "sequenceIndex": i,
                        },
                        status="done",
                    )
                    s.add(frame_ref_node)
                    s.flush()
                    spawned_nodes.append(frame_ref_node)

                    # FRAME FIDELITY: the source frame is passed as a reference
                    # image, but without this the model crops into a tight,
                    # smiling upper-body portrait and drops the rest of the body.
                    # Force it to reproduce the reference frame's exact shot:
                    # same camera distance / crop, full-body framing when the
                    # source is full-body (keep legs / lower body / full outfit
                    # in frame), same body pose and hand/arm position, and the
                    # same facial expression. Do NOT zoom in, do NOT add a smile.
                    frame_fidelity_prefix = (
                        "MATCH THE REFERENCE FRAME EXACTLY. Reproduce the same camera "
                        "distance, shot size, and crop as the sample frame: if the frame "
                        "is a full-body / knees-up shot, keep it full-body with the legs, "
                        "lower body, and full outfit visible — do NOT zoom into a tight "
                        "upper-body or face portrait. Keep the EXACT same outfit as the "
                        "frame, including any revealing or fitted clothing (crop top, "
                        "off-shoulder top, denim shorts, fitted dress) — never cover it "
                        "up, lengthen it, or swap it for more modest clothing. Keep the "
                        "same natural body shape and proportions, the same body pose, "
                        "stance, hand and arm position, body angle, and the same facial "
                        "expression as the frame (do NOT add a smile, open mouth, or a "
                        "different pose). Same framing, same composition, same energy. "
                    )
                    scene_image_prompt = f"{frame_fidelity_prefix}{image_prompt}"
                    if identity_prefix and identity_prefix not in scene_image_prompt:
                        scene_image_prompt = f"{identity_prefix}{scene_image_prompt}"
                    img_short_id = generate_unique_short_id(s, board_id)
                    img_node = Node(
                        board_id=board_id,
                        short_id=img_short_id,
                        type="image",
                        x=base_x + 640,
                        y=base_y + i * 240,
                        data={
                            "title": scene.get("title", f"Scene {i+1} - Image"),
                            "prompt": scene_image_prompt,
                            "aspectRatio": scene_frame.get("aspect_ratio") or "IMAGE_ASPECT_RATIO_PORTRAIT",
                            "sourceVideoFrameMediaId": scene_frame["media_id"],
                            "sequenceIndex": i,
                        },
                        status="idle",
                    )
                    s.add(img_node)
                    s.flush()

                    for ref_id in upstream_refs:
                        s.add(Edge(board_id=board_id, source_id=ref_id,
                                   target_id=img_node.id, kind="ref"))
                    s.add(Edge(board_id=board_id, source_id=frame_ref_node.id,
                               target_id=img_node.id, kind="ref"))
                    spawned_nodes.append(img_node)
                    scene_start_node = img_node
                    if base_img_node is None:
                        base_img_node = img_node

                    asset = s.exec(
                        select(Asset).where(Asset.uuid_media_id == scene_frame["media_id"])
                    ).first()
                    if asset and asset.node_id is None:
                        asset.node_id = frame_ref_node.id
                        s.add(asset)
                elif i == 0:
                    # Classic path: AI-generated base image for the first scene.
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
                            "title": scene.get("title", f"Cáº£nh {i+1} - áº¢nh"),
                            "prompt": first_image_prompt,
                            # Portrait by default to match the Video Assembly
                            # batch default (9:16 TikTok/Reels). Mismatched
                            # defaults made the batch flag every story image as
                            # aspect_mismatch and regenerate it needlessly.
                            "aspectRatio": "IMAGE_ASPECT_RATIO_PORTRAIT"
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
                    scene_start_node = img_node
                else:
                    scene_start_node = None

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
                    x=base_x + 960,
                    y=base_y + i * 240,
                    data={
                        "title": scene.get("title", f"Scene {i+1} - Clip"),
                        "prompt": combined_prompt,
                        "narration": scene.get("narration", ""),
                        "aspectRatio": "VIDEO_ASPECT_RATIO_PORTRAIT",
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

                # Each clip is anchored by its own generated scene image.
                # Sample-frame refs feed that image; videos never consume raw sample refs directly.
                video_start_node = scene_start_node or base_img_node
                if video_start_node is None:
                    raise HTTPException(400, "No generated image source available for video clip.")
                base_image_edge = Edge(
                    board_id=board_id,
                    source_id=video_start_node.id,
                    target_id=vid_node.id,
                    kind="ref",
                )
                s.add(base_image_edge)

                if prev_vid_node is not None:
                    chain_edge = Edge(
                        board_id=board_id,
                        source_id=prev_vid_node.id,
                        target_id=vid_node.id,
                        kind="ref",
                    )
                    s.add(chain_edge)

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


