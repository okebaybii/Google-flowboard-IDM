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
    # AI Director Panel fields
    style: Optional[str] = None  # "review", "entertainment", "cinematic", "dance", "education"
    shotDuration: Optional[int] = Field(default=8, ge=3, le=30)  # seconds per shot
    aspectRatio: Optional[str] = Field(default="portrait")  # "portrait" | "landscape" | "square"
    sceneCount: Optional[int] = Field(default=3, ge=1, le=10)  # number of scenes


class UpdateShotsRequest(BaseModel):
    shots: list[dict]


class CreateVideoRequest(BaseModel):
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
    """Phase 1: AI analyzes script → returns editable shots (saved in node.data.shots).

    Does NOT spawn image/video nodes. User reviews/edits shots first,
    then calls /story-script/{node_id}/create-video to spawn nodes.
    """
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

        # Director Panel params
        style = body.style or node_data.get("style", "cinematic")
        shot_duration = body.shotDuration or node_data.get("shotDuration", 8)
        aspect_ratio = body.aspectRatio or node_data.get("aspectRatio", "portrait")
        scene_count = body.sceneCount or node_data.get("sceneCount", 3)

        # Get LLM provider
        saved_providers = secrets.read_active_providers()
        provider_name = saved_providers.get("auto_prompt") or saved_providers.get("planner") or "gemini"

        provider = registry.get_provider(provider_name)
        if provider is None or not await provider.is_available():
            raise HTTPException(503, f"Configured LLM provider '{provider_name}' is not available. Please verify Settings.")

        # Gather upstream reference text
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

    # Style-specific instructions
    style_instructions = {
        "review": (
            "Phong cách: Review / Quảng cáo sản phẩm. Tập trung vào sản phẩm, cận cảnh chi tiết, "
            "góc quay chuyên nghiệp, ánh sáng studio. Narration mang tính đánh giá, so sánh, giới thiệu tính năng."
        ),
        "entertainment": (
            "Phong cách: Giải trí / Vlog. Tự nhiên, năng động, biểu cảm phong phú, "
            "góc quay linh hoạt. Narration vui vẻ, gần gũi, dễ đồng cảm."
        ),
        "cinematic": (
            "Phong cách: Cinematic / Phim ngắn. Ánh sáng điện ảnh, composition chuyên nghiệp, "
            "depth of field, color grading ấn tượng. Narration giàu cảm xúc, kịch tính."
        ),
        "dance": (
            "Phong cách: Dance / Trend. Nhịp điệu nhanh, chuyển động mượt, "
            "góc quay dynamic. Narration ngắn gọn, theo beat nhạc."
        ),
        "education": (
            "Phong cách: Giáo dục / Hướng dẫn. Rõ ràng, có cấu trúc, "
            "cận cảnh step-by-step. Narration giải thích dễ hiểu, tuần tự."
        ),
    }
    style_hint = style_instructions.get(style, style_instructions["cinematic"])

    aspect_map = {
        "portrait": "9:16 (dọc, TikTok/Reels)",
        "landscape": "16:9 (ngang, YouTube)",
        "square": "1:1 (vuông, Instagram)",
    }
    aspect_hint = aspect_map.get(aspect_ratio, aspect_map["portrait"])

    # Build the AI prompt — shots output format
    system_prompt = (
        "Bạn là một đạo diễn phim chuyên nghiệp và chuyên gia viết prompt AI. "
        "Phân tích kịch bản/ý tưởng được cung cấp và chia thành chuỗi phân cảnh (shots) liên tục.\n\n"
        f"CẤU HÌNH ĐẠO DIỄN:\n"
        f"- {style_hint}\n"
        f"- Thời lượng mỗi shot: {shot_duration} giây\n"
        f"- Tỷ lệ video: {aspect_hint}\n"
        f"- Số phân cảnh yêu cầu: {scene_count} cảnh\n\n"
        "Cho mỗi phân cảnh, bạn PHẢI cung cấp:\n"
        "1. title: Tiêu đề phân cảnh ngắn gọn bằng tiếng Việt\n"
        "2. narration: Lời thoại/voiceover bằng tiếng Việt (tối đa 2-3 câu). "
        "Viết tự nhiên, phù hợp phong cách. Đây là text sẽ được đọc bằng giọng AI.\n"
        "3. image_prompt: Prompt mô tả chi tiết khung hình đầu tiên của cảnh bằng tiếng Anh. "
        "Bao gồm: subject, lighting, colors, background, environment, framing (full-body, medium, close-up), "
        "pose, facial expression, outfit. Phải rất chi tiết để AI tạo hình ảnh chính xác.\n"
        "4. camera: Mô tả kỹ thuật camera và chuyển động bằng tiếng Anh. "
        "Ví dụ: 'Camera: Slow dolly in from medium shot to close-up. Shallow depth of field. "
        "Warm cinematic lighting from the left.'\n\n"
        "QUAN TRỌNG:\n"
        "- Mỗi cảnh phải kết nối liên tục với cảnh trước (không reset identity/location)\n"
        "- image_prompt phải bằng tiếng ANH, rất chi tiết\n"
        "- narration phải bằng tiếng VIỆT, tự nhiên\n"
        "- camera phải bằng tiếng ANH\n"
        "- title phải bằng tiếng VIỆT\n\n"
        "CỰC KỲ QUAN TRỌNG VỀ JSON: Output PHẢI là một mảng JSON (JSON array) hợp lệ. "
        "KHÔNG BAO GIỜ dùng dấu ngoặc kép (\") bên trong nội dung của các chuỗi (string) vì nó sẽ làm hỏng JSON. "
        "Nếu cần trích dẫn, hãy dùng dấu nháy đơn ('). Không markdown, không giải thích ngoài JSON."
    )

    full_prompt = f"{system_prompt}\n\n{reference_context}KỊCH BẢN:\n{prompt_text}\n\nJSON OUTPUT:"

    attachments = []
    sample_frame_candidates = []
    sample_video_url = body.sampleVideoUrl or (node.data or {}).get("sampleVideoUrl")
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
                'format': 'bestvideo[height<=1080]/best[height<=1080]/bestvideo/best',
                'quiet': True,
                'no_warnings': True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(sample_video_url, download=True)
                ext = info.get('ext', 'mp4')
                vid_path = os.path.join(temp_dir, f'video.{ext}')

            cap = cv2.VideoCapture(vid_path)
            fps = cap.get(cv2.CAP_PROP_FPS)
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

            MAX_CANDIDATE_FRAMES = 12
            if total_frames > 0:
                READ_BUDGET = 240
                read_stride = max(1, total_frames // READ_BUDGET)
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
                        rank = (1 if face_score > 0 else 0, sharpness)
                        prev = best.get(bucket)
                        if prev is None or rank > (1 if prev[1] > 0 else 0, prev[0]):
                            frame_copy = frame.copy() if hasattr(frame, "copy") else frame
                            best[bucket] = (sharpness, face_score, frame_copy, frac)
                    frame_count += 1

                for saved_count, bucket in enumerate(sorted(best.keys())):
                    sharpness, face_score, frame, frac = best[bucket]
                    import os as _os
                    frame_path = _os.path.join(temp_dir, f"frame_{saved_count}.jpg")
                    _write_jpeg(cv2, frame_path, frame)
                    if len(attachments) < 8:
                        attachments.append(frame_path)
                    shape = getattr(frame, "shape", None)
                    height = int(shape[0]) if shape is not None and len(shape) >= 2 else 0
                    width = int(shape[1]) if shape is not None and len(shape) >= 2 else 0
                    sample_frame_candidates.append({
                        "path": frame_path,
                        "width": width,
                        "height": height,
                        "face_score": face_score,
                        "frac": frac,
                    })
            cap.release()

            video_analysis_instruction = (
                "Hãy đóng vai một chuyên gia sản xuất video và bậc thầy viết prompt AI. "
                "Dựa vào video tôi cung cấp, hãy phân tích chi tiết từng khung hình và tạo prompt "
                "tiếng Anh để AI tạo sản phẩm tương tự.\n\n"
                "Trước khi viết prompt cho từng scene, bóc tách video theo cấu trúc sau:\n"
                "- Chủ thể chính: mô tả chi tiết ngoại hình, trang phục, đặc điểm nổi bật.\n"
                "- Hành động & Biểu cảm: tốc độ di chuyển, ngôn ngữ cơ thể.\n"
                "- Bối cảnh xung quanh: không gian, đồ vật phụ trợ, độ sâu trường ảnh.\n"
                "- Kỹ thuật quay: cỡ cảnh (Wide shot, Close-up, Medium shot), góc máy.\n"
                "- Ánh sáng & Màu sắc: loại ánh sáng, bảng màu chủ đạo.\n\n"
                "Sau khi phân tích, tổng hợp thành prompt tiếng Anh liên tục. "
                "Use those English prompts inside the JSON fields below."
            )
            full_prompt = (
                f"{system_prompt}\n\n{reference_context}"
                "I have attached keyframes from a reference video. "
                "You MUST analyze the visual motion, choreography, pose order, gesture timing, "
                "scene progression, background, props, camera, lighting, color palette, and production style objectively.\n\n"
                f"{video_analysis_instruction}\n\n"
                "IMPORTANT: Return ONLY the required JSON array.\n\n"
                f"KỊCH BẢN:\n{prompt_text}\n\nJSON OUTPUT:"
            )

        except Exception as e:
            logger.error(f"Failed to process sample video: {e}")
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)
            raise HTTPException(400, f"Không thể tải video mẫu: {e}")

    try:
        raw_result = await provider.run(full_prompt, attachments=attachments, timeout=120.0)
        clean_json = raw_result.strip()
        
        # Extract json block if inside markdown
        if "```json" in clean_json:
            clean_json = clean_json.split("```json")[1].split("```")[0]
        elif "```" in clean_json:
            clean_json = clean_json.split("```")[1].split("```")[0]
            
        # Try to find array brackets to strip surrounding text
        start_idx = clean_json.find("[")
        end_idx = clean_json.rfind("]")
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            clean_json = clean_json[start_idx:end_idx+1]
            
        import re
        # Strip trailing commas
        clean_json = re.sub(r",\s*([\]}])", r"\1", clean_json)
        
        # Simple fix for unescaped quotes inside strings (common LLM failure)
        # This is a bit risky but helps. Better rely on the LLM prompt constraint.
        clean_json = clean_json.strip()

        scenes = json.loads(clean_json)
        if not isinstance(scenes, list):
            raise ValueError("LLM output is not a JSON array")

    except Exception as exc:
        if isinstance(exc, HTTPException):
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)
            raise exc
        logger.error(f"LLM story generation failed: {exc}\nRaw result: {raw_result}")
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(400, f"Lỗi từ AI: {str(exc)}. Vui lòng tạo lại.")

    # Upload sample reference frames if available
    sample_reference = None
    scene_frame_refs: list[Optional[dict]] = []
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

        n_scenes = max(len(scenes), 1)
        uploaded_by_path: dict[str, Optional[dict]] = {}
        if sample_reference is not None:
            uploaded_by_path[chosen_frame["path"]] = sample_reference
        ordered = sorted(sample_frame_candidates, key=lambda f: f.get("frac", 0.0))
        for i in range(len(scenes)):
            target = (i + 0.5) / n_scenes
            by_distance = sorted(ordered, key=lambda f: abs(f.get("frac", 0.0) - target))
            scene_ref: Optional[dict] = None
            for cand in by_distance:
                path = cand["path"]
                if path not in uploaded_by_path:
                    uploaded_by_path[path] = await _upload_sample_reference_frame(
                        cand["path"],
                        project_id=body.projectId,
                        story_node_id=node_id,
                        width=cand["width"],
                        height=cand["height"],
                    )
                if uploaded_by_path[path] is not None:
                    scene_ref = uploaded_by_path[path]
                    break
            if scene_ref is None:
                scene_ref = sample_reference
            scene_frame_refs.append(scene_ref)

    if temp_dir:
        shutil.rmtree(temp_dir, ignore_errors=True)

    # Build editable shots array
    shots = []
    for i, scene in enumerate(scenes):
        shot = {
            "id": f"shot_{i:03d}",
            "index": i,
            "title": scene.get("title", f"Phân cảnh {i+1}"),
            "narration": scene.get("narration", ""),
            "image_prompt": _scene_text(scene, "image_prompt"),
            "video_prompt": _scene_text(scene, "video_prompt"),
            "camera": scene.get("camera", ""),
            "duration": shot_duration,
        }
        # Attach sample frame reference if available
        if i < len(scene_frame_refs) and scene_frame_refs[i]:
            shot["sampleFrameRef"] = scene_frame_refs[i]
        shots.append(shot)

    # Save shots + config to node.data (Phase 1 — editable, no nodes spawned yet)
    with get_session() as s:
        node = s.get(Node, node_id)
        if not node:
            raise HTTPException(404, "Node not found")
        updated_data = dict(node.data or {})
        updated_data["prompt"] = prompt_text
        updated_data["shots"] = shots
        updated_data["style"] = style
        updated_data["shotDuration"] = shot_duration
        updated_data["aspectRatio"] = aspect_ratio
        updated_data["sceneCount"] = scene_count
        if sample_reference:
            updated_data["sampleVideoReferenceMediaId"] = sample_reference["media_id"]
        node.data = updated_data
        node.status = "shots_ready"
        s.add(node)
        s.commit()

    return {
        "ok": True,
        "shots": shots,
        "scenes_count": len(shots),
    }


@router.post("/story-script/{node_id}/update-shots")
async def update_story_shots(node_id: int, body: UpdateShotsRequest):
    """Phase 2: Save user-edited shots back to node.data.shots."""
    with get_session() as s:
        node = s.get(Node, node_id)
        if not node:
            raise HTTPException(404, "Node not found")
        if node.type != "story_script":
            raise HTTPException(400, "Node must be of type 'story_script'")

        # Validate shots
        validated = []
        for i, shot in enumerate(body.shots):
            validated.append({
                "id": shot.get("id", f"shot_{i:03d}"),
                "index": i,
                "title": shot.get("title", f"Phân cảnh {i+1}"),
                "narration": shot.get("narration", ""),
                "image_prompt": shot.get("image_prompt", ""),
                "video_prompt": shot.get("video_prompt", ""),
                "camera": shot.get("camera", ""),
                "duration": shot.get("duration", 8),
                "sampleFrameRef": shot.get("sampleFrameRef"),
            })

        updated_data = dict(node.data or {})
        updated_data["shots"] = validated
        node.data = updated_data
        s.add(node)
        s.commit()

    return {"ok": True, "shots": validated}


@router.post("/story-script/{node_id}/create-video")
async def create_video_from_shots(node_id: int, body: CreateVideoRequest):
    """Phase 3: Spawn image + video nodes from finalized shots.

    Reads node.data.shots (edited by user), then spawns the same node
    graph that the old generate_story_script used to create in one step.
    Also connects upstream reference nodes and video_assembly.
    """
    spawned_nodes = []

    with get_session() as s:
        node = s.get(Node, node_id)
        if not node:
            raise HTTPException(404, "Node not found")
        if node.type != "story_script":
            raise HTTPException(400, "Node must be of type 'story_script'")

        node_data = node.data or {}
        shots = node_data.get("shots", [])
        if not shots:
            raise HTTPException(400, "No shots found. Generate shots first.")

        board_id = node.board_id
        aspect_ratio = node_data.get("aspectRatio", "portrait")
        sample_video_url = node_data.get("sampleVideoUrl", "")

        # Map aspect ratio
        img_aspect = {
            "portrait": "IMAGE_ASPECT_RATIO_PORTRAIT",
            "landscape": "IMAGE_ASPECT_RATIO_LANDSCAPE",
            "square": "IMAGE_ASPECT_RATIO_SQUARE",
        }.get(aspect_ratio, "IMAGE_ASPECT_RATIO_PORTRAIT")
        vid_aspect = {
            "portrait": "VIDEO_ASPECT_RATIO_PORTRAIT",
            "landscape": "VIDEO_ASPECT_RATIO_LANDSCAPE",
            "square": "VIDEO_ASPECT_RATIO_SQUARE",
        }.get(aspect_ratio, "VIDEO_ASPECT_RATIO_PORTRAIT")

        # Update story_script node status to running
        node.status = "running"
        s.add(node)
        s.commit()

    try:
        with get_session() as s:
            node = s.get(Node, node_id)

            # Find connected video_assembly node
            edges = s.exec(select(Edge).where(Edge.source_id == node_id)).all()
            assembly_node_id = None
            for e in edges:
                target_node = s.get(Node, e.target_id)
                if target_node and target_node.type == "video_assembly":
                    assembly_node_id = target_node.id
                    break

            if not assembly_node_id:
                assembly_node = s.exec(
                    select(Node).where(Node.board_id == board_id, Node.type == "video_assembly")
                ).first()
                if assembly_node:
                    assembly_node_id = assembly_node.id

            # Find upstream reference nodes
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

            if assembly_node_id:
                incoming_assembly_edges = s.exec(
                    select(Edge).where(Edge.target_id == assembly_node_id)
                ).all()
                for se in incoming_assembly_edges:
                    sn = s.get(Node, se.source_id)
                    if sn and sn.type in allowed_ref_types:
                        if sn.id not in upstream_refs:
                            upstream_refs.append(sn.id)

            # Fallback: style_preset on board
            has_style = any(s.get(Node, rid).type == "style_preset" for rid in upstream_refs if s.get(Node, rid))
            if not has_style:
                board_presets = s.exec(
                    select(Node).where(Node.board_id == board_id, Node.type == "style_preset")
                ).all()
                for bp in board_presets:
                    if bp.id not in upstream_refs:
                        upstream_refs.append(bp.id)

            # Fallback: character on board
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

            character_ref_ids = [
                ref_id for ref_id in upstream_refs if _node_type(ref_id) == "character"
            ]
            identity_prefix = ""
            if character_ref_ids:
                identity_prefix = (
                    "Use the connected Character reference as the authoritative identity source. "
                    "REFERENCE PRIORITY: Character reference is face/identity ONLY. "
                    "Preserve the exact same Character face, facial features, hairstyle, age, "
                    "skin tone, and overall person. "
                )
            elif sample_video_url:
                identity_prefix = (
                    "Use the connected sample video reference as the authoritative "
                    "identity source: preserve the same face, hairstyle, outfit, body "
                    "proportions, and setting. "
                )

            prev_vid_node = None
            base_img_node = None

            for i, shot in enumerate(shots):
                image_prompt = shot.get("image_prompt", "")
                video_prompt = shot.get("video_prompt", "")
                narration = shot.get("narration", "")
                camera = shot.get("camera", "")
                sample_frame_ref = shot.get("sampleFrameRef")

                if sample_frame_ref:
                    # Per-scene sample frame reference node
                    frame_short_id = generate_unique_short_id(s, board_id)
                    frame_ref_node = Node(
                        board_id=board_id,
                        short_id=frame_short_id,
                        type="visual_asset",
                        x=base_x + 320,
                        y=base_y + i * 240,
                        data={
                            "title": shot.get("title", f"Scene {i+1} - Sample frame"),
                            "prompt": (
                                "Timeline sample frame reference. Preserve this frame's background, "
                                "composition, camera angle, lighting, outfit/body pose, and scene style."
                            ),
                            "aiBrief": "Per-scene sample frame used as background/pose/camera reference.",
                            "mediaId": sample_frame_ref["media_id"],
                            "mediaIds": [sample_frame_ref["media_id"]],
                            "variantCount": 1,
                            "aspectRatio": sample_frame_ref.get("aspect_ratio") or img_aspect,
                            "sourceVideoFrame": True,
                            "sourceStoryScriptId": node_id,
                            "sequenceIndex": i,
                        },
                        status="done",
                    )
                    s.add(frame_ref_node)
                    s.flush()
                    spawned_nodes.append(frame_ref_node)

                    scene_image_prompt = f"{identity_prefix}{image_prompt}" if identity_prefix else image_prompt
                    if camera:
                        scene_image_prompt += f" {camera}"
                    img_short_id = generate_unique_short_id(s, board_id)
                    img_node = Node(
                        board_id=board_id,
                        short_id=img_short_id,
                        type="image",
                        x=base_x + 640,
                        y=base_y + i * 240,
                        data={
                            "title": shot.get("title", f"Scene {i+1} - Image"),
                            "prompt": scene_image_prompt,
                            "aspectRatio": sample_frame_ref.get("aspect_ratio") or img_aspect,
                            "sourceVideoFrameMediaId": sample_frame_ref["media_id"],
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
                        select(Asset).where(Asset.uuid_media_id == sample_frame_ref["media_id"])
                    ).first()
                    if asset and asset.node_id is None:
                        asset.node_id = frame_ref_node.id
                        s.add(asset)
                elif i == 0:
                    # First scene: AI-generated base image
                    first_image_prompt = image_prompt
                    if identity_prefix and identity_prefix not in first_image_prompt:
                        first_image_prompt = f"{identity_prefix}{first_image_prompt}"
                    if camera:
                        first_image_prompt += f" {camera}"
                    img_short_id = generate_unique_short_id(s, board_id)
                    img_node = Node(
                        board_id=board_id,
                        short_id=img_short_id,
                        type="image",
                        x=base_x + 320,
                        y=base_y - 100,
                        data={
                            "title": shot.get("title", f"Cảnh {i+1} - Ảnh"),
                            "prompt": first_image_prompt,
                            "aspectRatio": img_aspect,
                        },
                        status="idle",
                    )
                    s.add(img_node)
                    s.flush()

                    for ref_id in upstream_refs:
                        s.add(Edge(board_id=board_id, source_id=ref_id,
                                   target_id=img_node.id, kind="ref"))

                    spawned_nodes.append(img_node)
                    base_img_node = img_node
                    scene_start_node = img_node
                else:
                    # Non-first scene without sample frame: generate its own image
                    per_scene_prompt = image_prompt
                    if identity_prefix and identity_prefix not in per_scene_prompt:
                        per_scene_prompt = f"{identity_prefix}{per_scene_prompt}"
                    if camera:
                        per_scene_prompt += f" {camera}"
                    img_short_id = generate_unique_short_id(s, board_id)
                    img_node = Node(
                        board_id=board_id,
                        short_id=img_short_id,
                        type="image",
                        x=base_x + 640,
                        y=base_y + i * 240,
                        data={
                            "title": shot.get("title", f"Scene {i+1} - Image"),
                            "prompt": per_scene_prompt,
                            "aspectRatio": img_aspect,
                            "sequenceIndex": i,
                        },
                        status="idle",
                    )
                    s.add(img_node)
                    s.flush()
                    for ref_id in upstream_refs:
                        s.add(Edge(board_id=board_id, source_id=ref_id,
                                   target_id=img_node.id, kind="ref"))
                    spawned_nodes.append(img_node)
                    scene_start_node = img_node
                    if base_img_node is None:
                        base_img_node = img_node

                # Build video prompt with continuity
                combined_prompt = _build_continuity_video_prompt(
                    scene_index=i,
                    total_scenes=len(shots),
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
                        "title": shot.get("title", f"Scene {i+1} - Clip"),
                        "prompt": combined_prompt,
                        "narration": narration,
                        "aspectRatio": vid_aspect,
                        "sourceImagePrompt": image_prompt,
                        "sourceVideoPrompt": video_prompt,
                        "camera": camera,
                        "sequenceIndex": i,
                        "sequenceTotal": len(shots),
                        "continuityMode": "chain",
                        "requiresPreviousClip": i > 0,
                        "fallbackStartImage": i > 0,
                        "shotDuration": shot.get("duration", 8),
                    },
                    status="idle",
                )
                s.add(vid_node)
                s.flush()

                video_start_node = scene_start_node or base_img_node
                if video_start_node is None:
                    raise HTTPException(400, "No generated image source available for video clip.")
                s.add(Edge(board_id=board_id, source_id=video_start_node.id,
                           target_id=vid_node.id, kind="ref"))

                if prev_vid_node is not None:
                    s.add(Edge(board_id=board_id, source_id=prev_vid_node.id,
                               target_id=vid_node.id, kind="ref"))

                prev_vid_node = vid_node
                spawned_nodes.append(vid_node)

                # Connect video to assembly
                if assembly_node_id:
                    s.add(Edge(board_id=board_id, source_id=vid_node.id,
                               target_id=assembly_node_id, kind="ref"))

            # Update story_script node status to done
            node.status = "done"
            s.add(node)
            s.commit()

            return {
                "ok": True,
                "scenes_count": len(shots),
                "spawned_nodes": [n.id for n in spawned_nodes],
            }
    except Exception as e:
        with get_session() as s:
            node = s.get(Node, node_id)
            if node:
                node.status = "error"
                s.add(node)
                s.commit()
        raise HTTPException(500, f"Error spawning storyboard nodes: {str(e)}")

