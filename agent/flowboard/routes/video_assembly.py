"""Video Assembly routes for merging video clips and adding background audio.

Concatenates multiple upstream video node clips and overlays an uploaded audio file.
"""
import uuid
import logging
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from sqlmodel import select

from flowboard.db import get_session
from flowboard.db.models import Node, Edge, Asset
from flowboard.config import STORAGE_DIR
from flowboard.services import media as media_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/video-assembly", tags=["video-assembly"])


@router.post("/upload-audio")
async def upload_audio(
    node_id: Optional[int] = Form(default=None),
    file: UploadFile = File(...)
):
    """Upload a background audio file (supporting .mp3, .wav, .m4a, .aac, .flac, .ogg, etc.) for assembly."""
    mime = (file.content_type or "").lower().split(";")[0].strip()
    
    # Rộng lượng hóa kiểm tra định dạng
    allowed_exts = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".wma", ".opus")
    is_valid_audio = mime.startswith("audio/") or file.filename.lower().endswith(allowed_exts)
    
    if not is_valid_audio:
        raise HTTPException(
            status_code=415,
            detail="Unsupported audio format. Please upload a valid audio file (e.g., .mp3, .wav, .m4a, .flac, .aac)."
        )
        
    raw = await file.read()
    if len(raw) == 0:
        raise HTTPException(status_code=400, detail="Empty audio file")
        
    # Trích xuất phần mở rộng chính xác của file
    suffix = Path(file.filename).suffix.lower()
    ext = suffix if suffix in allowed_exts else ".mp3"
        
    output_media_id = str(uuid.uuid4())
    MEDIA_CACHE_DIR = STORAGE_DIR / "media"
    MEDIA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = MEDIA_CACHE_DIR / f"{output_media_id}{ext}"
    
    try:
        cache_path.write_bytes(raw)
    except OSError as exc:
        logger.error(f"Failed to write audio cache: {exc}")
        raise HTTPException(status_code=500, detail="Failed to save audio file")
        
    with get_session() as session:
        asset = Asset(
            uuid_media_id=output_media_id,
            kind="audio",
            mime=mime or "audio/mpeg",
            local_path=str(cache_path),
            node_id=node_id
        )
        session.add(asset)
        session.commit()
        
    return {
        "media_id": output_media_id,
        "mime": mime or "audio/mpeg",
        "size": len(raw),
        "filename": file.filename
    }


class AssembleRequest(BaseModel):
    video_order: List[str]
    audio_media_id: Optional[str] = None


from proglog import ProgressBarLogger
import time

class DBProgressBarLogger(ProgressBarLogger):
    def __init__(self, node_id: int):
        super().__init__()
        self.node_id = node_id
        self.last_progress = 0
        self.last_update_time = 0.0

    def bars_callback(self, bar, attr, value, old_value=None):
        if attr == "index":
            total = self.bars.get(bar, {}).get("total", 1) or 1
            progress = int((value / total) * 100)
            progress = max(0, min(100, progress))
            
            current_time = time.time()
            if progress != self.last_progress and (
                progress - self.last_progress >= 2 
                or current_time - self.last_update_time >= 1.0 
                or progress == 100
            ):
                self.last_progress = progress
                self.last_update_time = current_time
                self._update_progress_in_db(progress)

    def _update_progress_in_db(self, progress: int):
        from flowboard.db import get_session
        from flowboard.db.models import Node
        try:
            with get_session() as session:
                node = session.get(Node, self.node_id)
                if node:
                    node_data = dict(node.data)
                    node_data["assemblyProgress"] = progress
                    node.data = node_data
                    session.add(node)
                    session.commit()
        except Exception:
            pass


def _run_moviepy_assembly(
    video_paths: List[str],
    narrations: List[str],
    audio_path: Optional[str],
    output_path: str,
    node_id: Optional[int] = None
) -> None:
    """Concatenate videos and overlay audio + dynamic TTS narration using MoviePy.
    
    This function executes in a separate thread to prevent blocking the async loop.
    """
    from moviepy import VideoFileClip, concatenate_videoclips, AudioFileClip, CompositeAudioClip
    from gtts import gTTS
    import os

    clips = []
    tts_audio_clips = []
    temp_files = []
    try:
        # 1. Load video clips and generate aligned TTS narration audio
        current_offset = 0.0
        for i, path in enumerate(video_paths):
            clip = VideoFileClip(path)
            clips.append(clip)
            
            # Kiểm tra nếu phân cảnh này có lời thoại thuyết minh
            narration_text = narrations[i] if i < len(narrations) else ""
            if narration_text and narration_text.strip():
                try:
                    # Tạo file âm thanh thuyết minh tạm thời
                    temp_tts_path = f"temp_tts_{i}_{os.getpid()}.mp3"
                    tts = gTTS(text=narration_text.strip(), lang="vi")
                    tts.save(temp_tts_path)
                    temp_files.append(temp_tts_path)
                    
                    # Nạp audio thuyết minh và thiết lập bắt đầu khớp thời lượng phân cảnh
                    tts_clip = AudioFileClip(temp_tts_path)
                    tts_clip = tts_clip.with_start(current_offset)
                    tts_audio_clips.append(tts_clip)
                except Exception as tts_err:
                    logger.error(f"Failed to generate TTS for scene {i}: {tts_err}")
                    
            # Tăng mốc offset thời gian để khớp phân cảnh tiếp theo
            current_offset += clip.duration
        
        # 2. Concatenate video clips
        final_clip = concatenate_videoclips(clips, method="compose")
        video_duration = final_clip.duration
        
        # 3. Mix audio components (Background Music at 20% volume + TTS Voiceover)
        audio_components = []
        
        # Thêm nhạc nền nếu có (giảm âm lượng xuống 20% để giọng thoại rõ ràng)
        if audio_path:
            bg_music = AudioFileClip(audio_path)
            if bg_music.duration > video_duration:
                bg_music = bg_music.subclipped(0, video_duration)
            else:
                bg_music = bg_music.with_duration(video_duration)
            
            bg_music = bg_music.volumex(0.2)
            audio_components.append(bg_music)
            
        # Thêm toàn bộ danh sách giọng thuyết minh AI
        if tts_audio_clips:
            audio_components.extend(tts_audio_clips)
            
        # Gộp tất cả âm thanh lại
        if audio_components:
            final_audio = CompositeAudioClip(audio_components)
            final_clip = final_clip.with_audio(final_audio)
            
        # 4. Write output file
        logger_obj = DBProgressBarLogger(node_id) if node_id is not None else None
        final_clip.write_videofile(
            output_path,
            codec="libx264",
            audio_codec="aac",
            temp_audiofile="temp-audio.m4a",
            remove_temp=True,
            logger=logger_obj
        )
        
        # Close to free file handles
        final_clip.close()
        
    finally:
        # Giải phóng tài nguyên
        for c in clips:
            try:
                c.close()
            except Exception:
                pass
        for ac in tts_audio_clips:
            try:
                ac.close()
            except Exception:
                pass
        # Xóa các file nhạc thuyết minh tạm thời
        for temp_file in temp_files:
            try:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            except Exception as clean_err:
                logger.error(f"Failed to clean up temp TTS file {temp_file}: {clean_err}")
                c.close()
            except Exception:
                pass


async def _assemble_videos_impl(
    node_id: int,
    video_order: List[str],
    audio_media_id: Optional[str]
) -> dict:
    """Concatenate connected video nodes and overlay background audio."""
    try:
        with get_session() as session:
            # 1. Verify target Node exists
            node = session.get(Node, node_id)
            if not node:
                raise HTTPException(status_code=404, detail="Node not found")
                
            if node.type != "video_assembly":
                raise HTTPException(status_code=400, detail="Node must be of type 'video_assembly'")
                
            # 2. Find all connected upstream nodes
            edges = session.exec(
                select(Edge).where(Edge.target_id == node_id)
            ).all()
            
            upstream_node_ids = [e.source_id for e in edges]
            if not upstream_node_ids:
                raise HTTPException(status_code=400, detail="No connected nodes found. Please connect some video nodes first.")
                
            upstream_nodes = session.exec(
                select(Node).where(Node.id.in_(upstream_node_ids))
            ).all()
            
            # Filter only "video" nodes
            video_nodes = [n for n in upstream_nodes if n.type == "video"]
            if not video_nodes:
                raise HTTPException(status_code=400, detail="No connected 'video' nodes found.")
                
            # 3. Sort nodes
            # Sort by their position in video_order, or layout x coordinate if not in order array
            def sort_key(n: Node):
                node_rf_id = str(n.id)
                if node_rf_id in video_order:
                    return (0, video_order.index(node_rf_id))
                return (1, n.x)
                
            video_nodes.sort(key=sort_key)
            
            # 4. Resolve cached media file paths and narrations
            video_paths = []
            narrations = []
            for vn in video_nodes:
                media_id = vn.data.get("mediaId")
                if not media_id:
                    continue
                path = media_service.cached_path(media_id)
                if path and path.exists():
                    video_paths.append(str(path))
                    narrations.append(vn.data.get("narration", ""))
                    
            if not video_paths:
                raise HTTPException(status_code=400, detail="Connected videos have not been generated yet. Please generate them first.")
                
            # 5. Check background audio
            audio_path = None
            if audio_media_id:
                path = media_service.cached_path(audio_media_id)
                if path and path.exists():
                    audio_path = str(path)
                else:
                    raise HTTPException(status_code=400, detail=f"Audio asset '{audio_media_id}' not found.")
            
            # 6. Setup output path
            output_media_id = str(uuid.uuid4())
            MEDIA_CACHE_DIR = STORAGE_DIR / "media"
            MEDIA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            output_path = MEDIA_CACHE_DIR / f"{output_media_id}.mp4"
            
            # Update node status to "running" and initialize progress
            node.status = "running"
            node_data = dict(node.data)
            node_data["assemblyProgress"] = 0
            node.data = node_data
            session.add(node)
            session.commit()
            session.refresh(node)
            
        # 7. Run compilation in separate thread (passing aligned narrations and node_id)
        await asyncio.to_thread(
            _run_moviepy_assembly,
            video_paths,
            narrations,
            audio_path,
            str(output_path),
            node_id
        )
        
        with get_session() as session:
            # Re-fetch node in new transaction
            node = session.get(Node, node_id)
            
            # 8. Register new Asset row
            asset = Asset(
                uuid_media_id=output_media_id,
                kind="video",
                mime="video/mp4",
                local_path=str(output_path),
                node_id=node_id
            )
            session.add(asset)
            
            # 9. Update Node data
            node_data = dict(node.data)
            node_data["mediaId"] = output_media_id
            node_data["mediaIds"] = [output_media_id]
            node_data["variantCount"] = 1
            node_data["aspectRatio"] = "16:9"  # Default aspect for compiled videos
            node_data["audioMediaId"] = audio_media_id
            node_data["videoOrder"] = video_order
            node_data["assemblyProgress"] = 100
            
            node.data = node_data
            node.status = "done"
            session.add(node)
            session.commit()
            
            return {
                "ok": True,
                "mediaId": output_media_id,
                "nodeId": node_id,
                "status": "done"
            }
            
    except HTTPException as he:
        # Revert status to error/idle
        with get_session() as s:
            node = s.get(Node, node_id)
            if node:
                node.status = "error"
                node_data = dict(node.data)
                node_data["assemblyProgress"] = 0
                node.data = node_data
                s.add(node)
                s.commit()
        raise he
        
    except Exception as e:
        logger.error(f"Error compiling video assembly node: {str(e)}")
        with get_session() as s:
            node = s.get(Node, node_id)
            if node:
                node.status = "error"
                node_data = dict(node.data)
                node_data["assemblyProgress"] = 0
                node.data = node_data
                s.add(node)
                s.commit()
        raise HTTPException(status_code=500, detail=f"Assembly failed: {str(e)}")


@router.post("/node/{node_id}/assemble")
async def assemble_videos(node_id: int, req: AssembleRequest):
    """Concatenate connected video nodes and overlay background audio."""
    return await _assemble_videos_impl(node_id, req.video_order, req.audio_media_id)


class GenerateAllRequest(BaseModel):
    paygate_tier: Optional[str] = None
    image_model: Optional[str] = None
    video_quality: Optional[str] = None
    video_model: Optional[str] = None
    omni_flash_duration: Optional[int] = None
    auto_assemble: bool = False
    batch_video_aspect_ratio: Optional[str] = None
    batch_camera_mode: Optional[str] = None
    retry_failed: bool = False


STYLE_PROMPTS = {
    "hollywood": ", 35mm anamorphic lens, hollywood cinematic film style, dramatic lighting, color graded, highly detailed, photorealistic",
    "ghibli": ", Studio Ghibli anime style, hand-drawn look, detailed watercolor scenery, aesthetic retro anime, nostalgic, masterfully crafted art",
    "pixar": ", Pixar 3D animation style, cute character design, soft glossy lighting, clay texture, vibrant colors, detailed models",
    "cyberpunk": ", cyberpunk cinematic neon style, futuristic sci-fi movie scene, blue and purple neon glowing highlights, rain reflections, highly detailed",
    "comic": ", classic comic book style, hand-drawn ink lines, retro print halftone texture, bold colors, action pose",
    "noir": ", vintage 1940s film noir style, monochrome retro black and white cinematic, dramatic high contrast shadows, classic vintage cinematography",
    "real_life": ", photorealistic real life style, daily life cinematography, natural soft lighting, high-fidelity details, sharp focus, captured on professional 8k camera",
    "ancient_china": ", ancient Chinese historical film style, traditional Hanfu costume, beautiful cinematic dynamic lighting, wuxia aesthetic style, atmospheric, highly detailed",
    "xuanhuan": ", Chinese Xuanhuan fantasy style, glowing cultivation magic aura, epic mythical floating mountains, hyperdetailed CGI, celestial color grading, majestic",
    "product_review": ", professional product review styling, commercial product photography, cinematic soft studio lighting, high fidelity details, sharp focus, clean modern product catalog aesthetics",
    "tiktok_dance": ", vertical TikTok style vlog footage, dynamic handheld camera, bright urban indoor lighting, colorful aesthetic bedroom background, smooth motion, high frame rate, social media video look",
    "cartoon_style": ", beautiful 2D cartoon animation style, clean outlines, colorful cel shading, whimsical and charming cartoon aesthetics, highly detailed vector art",
}


from collections import defaultdict
from flowboard.db.models import BoardFlowProject, Request
from flowboard.worker.processor import get_worker
from fastapi import BackgroundTasks

async def _await_request(
    request_id: int,
    timeout_s: float = 300.0,
    poll_s: float = 1.5,
) -> Request:
    elapsed = 0.0
    while elapsed < timeout_s:
        await asyncio.sleep(poll_s)
        elapsed += poll_s
        with get_session() as s:
            row = s.get(Request, request_id)
            if row is None:
                raise RuntimeError(f"request {request_id} disappeared")
            if row.status in ("done", "failed", "timeout", "canceled"):
                return row
    raise asyncio.TimeoutError()


def _edge_variant_idx(edge: Edge) -> Optional[int]:
    """Return the per-edge variant pin if present.

    Older DB rows may expose the pin as the SQLModel field
    `source_variant_idx`. Some frontend payloads/comments call it
    `sourceVariantIdx`, so accept both defensively.
    """
    raw = getattr(edge, "source_variant_idx", None)
    if raw is None:
        raw = getattr(edge, "sourceVariantIdx", None)
    if isinstance(raw, int) and raw >= 0:
        return raw
    return None


def _select_node_media_id(node: Node, edge: Optional[Edge] = None) -> Optional[str]:
    """Choose the media id from a node using Flowboard's variant rules."""
    data = node.data or {}
    variants = data.get("mediaIds")
    pinned = _edge_variant_idx(edge) if edge is not None else None
    if isinstance(variants, list) and pinned is not None and pinned < len(variants):
        chosen = variants[pinned]
        if isinstance(chosen, str) and chosen:
            return chosen
    active = data.get("mediaId")
    if isinstance(active, str) and active:
        return active
    if isinstance(variants, list) and variants:
        first = variants[0]
        if isinstance(first, str) and first:
            return first
    return None


def _collect_reference_media_ids(
    node_id: int,
    node_map: dict[int, Node],
    incoming_edges: dict[int, list[Edge]],
    allowed_types: set[str],
) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for edge in incoming_edges.get(node_id, []):
        src = node_map.get(edge.source_id)
        if not src or src.type not in allowed_types:
            continue
        media_id = _select_node_media_id(src, edge)
        if media_id and media_id not in seen:
            refs.append(media_id)
            seen.add(media_id)
    return refs


def _batch_camera_suffix(camera_mode: Optional[str]) -> str:
    if camera_mode == "static":
        return " Camera: locked-off tripod shot, no pan, no zoom, no dolly; keep framing stable while subjects may move naturally."
    if camera_mode == "cinematic":
        return " Camera: cinematic controlled movement with smooth dolly, pan, or parallax; avoid chaotic handheld shake."
    if camera_mode == "dynamic":
        return " Camera: subtle smooth movement, gentle pan or slow dolly/zoom only; keep subject readable and stable."
    return ""


def _friendly_generation_error(error: Optional[str]) -> str:
    raw = (error or "").lower()
    if "internal error" in raw:
        return "Flow/Google video bị lỗi nội bộ. Hãy thử tạo lại clip, hoặc đổi prompt/camera/aspect nhẹ hơn."
    if "quota" in raw or "resource has been exhausted" in raw:
        return "Hết quota/credit tạo video. Hãy kiểm tra tài khoản hoặc thử lại sau."
    if "unsafe" in raw or "sexual" in raw or "public_error" in raw:
        return "Request có thể bị bộ lọc nội dung chặn. Hãy chỉnh prompt an toàn hơn."
    if "missing_upstream_image" in raw:
        return "Video thiếu ảnh đầu vào. Hãy nối video node với image/storyboard đã tạo xong."
    if "timeout" in raw:
        return "Tạo video quá lâu và bị timeout. Hãy thử lại hoặc giảm độ phức tạp prompt."
    return "Tạo clip thất bại. Hãy thử lại hoặc kiểm tra prompt/reference đầu vào."


async def run_batch_generation(
    assembly_node_id: int,
    project_id: str,
    paygate_tier: str,
    image_model: Optional[str] = None,
    video_quality: Optional[str] = None,
    video_model: Optional[str] = None,
    omni_flash_duration: Optional[int] = None,
    batch_video_aspect_ratio: Optional[str] = None,
    batch_camera_mode: Optional[str] = None,
    retry_failed: bool = False,
    auto_assemble: bool = False,
):
    logger.info(f"Starting batch generation for assembly node {assembly_node_id}")
    batch_video_aspect_ratio = batch_video_aspect_ratio or "VIDEO_ASPECT_RATIO_PORTRAIT"
    batch_camera_mode = batch_camera_mode or "dynamic"
    try:
        with get_session() as session:
            node = session.get(Node, assembly_node_id)
            if not node:
                logger.error(f"Assembly node {assembly_node_id} not found")
                return
            board_id = node.board_id
            
            all_nodes = session.exec(select(Node).where(Node.board_id == board_id)).all()
            all_edges = session.exec(select(Edge).where(Edge.board_id == board_id)).all()
            
        node_map = {n.id: n for n in all_nodes}
        
        # Adjacency: target -> list of incoming edges. Keep the full Edge
        # object so per-edge variant pins are available when selecting media.
        incoming = defaultdict(list)
        for e in all_edges:
            incoming[e.target_id].append(e)
            
        # BFS to find all upstream nodes recursively
        visited = set()
        queue = [assembly_node_id]
        while queue:
            curr = queue.pop(0)
            for edge in incoming[curr]:
                src_id = edge.source_id
                if src_id not in visited:
                    visited.add(src_id)
                    queue.append(src_id)
                    
        upstream_nodes = [node_map[nid] for nid in visited if nid in node_map]
        
        # Filter nodes that need generation
        gen_nodes = [n for n in upstream_nodes if n.type in ("image", "video", "Storyboard")]
        
        # Topological sort
        in_count = {n.id: 0 for n in gen_nodes}
        for e in all_edges:
            if e.source_id in in_count and e.target_id in in_count:
                in_count[e.target_id] += 1
                
        ready = [nid for nid, c in in_count.items() if c == 0]
        order = []
        seen = set()
        
        forward = defaultdict(list)
        for e in all_edges:
            if e.source_id in in_count and e.target_id in in_count:
                forward[e.source_id].append(e.target_id)
                
        while ready:
            nid = ready.pop(0)
            if nid in seen:
                continue
            seen.add(nid)
            order.append(nid)
            for child in forward[nid]:
                in_count[child] -= 1
                if in_count[child] <= 0:
                    ready.append(child)
                    
        for n in gen_nodes:
            if n.id not in seen:
                order.append(n.id)
                
        logger.info(f"Topological order for batch: {order}")
        
        failed_nodes = set()
        
        for nid in order:
            with get_session() as session:
                node = session.get(Node, nid)
                if not node:
                    continue
                
                # Skip if already done
                media_id = node.data.get("mediaId")
                if node.status == "done" and media_id:
                    logger.info(f"Node {nid} is already done. Skipping.")
                    continue
                if node.status == "error" and media_id and not retry_failed:
                    logger.info(f"Node {nid} is error but already has media. Skipping.")
                    failed_nodes.add(nid)
                    continue
                    
                # Upstream failure check
                parent_edges = incoming[nid]
                parent_ids = [edge.source_id for edge in parent_edges]
                upstream_failed = any(p in failed_nodes for p in parent_ids)
                if upstream_failed:
                    failed_nodes.add(nid)
                    node.status = "error"
                    node.data = {**dict(node.data), "error": "upstream_failed"}
                    session.add(node)
                    session.commit()
                    continue
                    
                prompt = node.data.get("prompt", "").strip()
                if not prompt:
                    failed_nodes.add(nid)
                    node.status = "error"
                    node.data = {**dict(node.data), "error": "missing_prompt"}
                    session.add(node)
                    session.commit()
                    continue
                    
                # Check style preset target
                style_edges = session.exec(
                    select(Edge).where(Edge.target_id == nid)
                ).all()
                style_node = None
                for se in style_edges:
                    sn = session.get(Node, se.source_id)
                    if sn and sn.type == "style_preset":
                        style_node = sn
                        break
                
                final_prompt = prompt
                if style_node:
                    style_id = style_node.data.get("activeStyleId", "hollywood")
                    suffix = STYLE_PROMPTS.get(style_id, "")
                    if suffix:
                        final_prompt = f"{prompt}{suffix}"
                
                # Build dispatch params. Match the manual generation path as
                # closely as possible so batch output keeps the same character,
                # visual asset, style, and pinned variant context.
                ref_source_types = {"character", "image", "visual_asset", "Storyboard"}
                if node.type in ("image", "Storyboard"):
                    upstream_refs = _collect_reference_media_ids(
                        nid,
                        node_map,
                        incoming,
                        ref_source_types,
                    )

                    params = {
                        "prompt": final_prompt,
                        "project_id": project_id,
                        "aspect_ratio": node.data.get("aspectRatio") or "IMAGE_ASPECT_RATIO_LANDSCAPE",
                        "paygate_tier": paygate_tier,
                        "variant_count": node.data.get("variantCount") or 1,
                        "image_model": node.data.get("imageModel") or image_model or "NANO_BANANA_2",
                    }
                    if upstream_refs:
                        params["ref_media_ids"] = upstream_refs
                    prompts = node.data.get("prompts")
                    if isinstance(prompts, list) and prompts:
                        params["prompts"] = prompts
                    req_type = "gen_image"
                else:  # video
                    video_model_value = node.data.get("videoModel") or node.data.get("model") or video_model or "veo"
                    is_omni = video_model_value == "omni_flash"
                    if is_omni:
                        ref_media_ids = _collect_reference_media_ids(
                            nid,
                            node_map,
                            incoming,
                            ref_source_types,
                        )
                        if not ref_media_ids:
                            failed_nodes.add(nid)
                            node.status = "error"
                            node.data = {**dict(node.data), "error": "missing_ref_media_ids"}
                            session.add(node)
                            session.commit()
                            continue
                        params = {
                            "prompt": f"{final_prompt}{_batch_camera_suffix(batch_camera_mode)}",
                            "project_id": project_id,
                            "ref_media_ids": ref_media_ids,
                            "duration_s": node.data.get("durationS") or node.data.get("omniFlashDuration") or omni_flash_duration or 4,
                            "aspect_ratio": node.data.get("aspectRatio") or batch_video_aspect_ratio,
                            "paygate_tier": paygate_tier,
                        }
                        req_type = "gen_video_omni"
                    else:
                        start_media_ids = []
                        seen_start_ids = set()
                        for edge in parent_edges:
                            p_node = node_map.get(edge.source_id)
                            if p_node and p_node.type in ("image", "Storyboard"):
                                mid = _select_node_media_id(p_node, edge)
                                if mid and mid not in seen_start_ids:
                                    start_media_ids.append(mid)
                                    seen_start_ids.add(mid)
                        if not start_media_ids:
                            failed_nodes.add(nid)
                            node.status = "error"
                            node.data = {
                                **dict(node.data),
                                "error": "missing_upstream_image",
                                "errorHint": _friendly_generation_error("missing_upstream_image"),
                            }
                            session.add(node)
                            session.commit()
                            continue

                        params = {
                            "prompt": f"{final_prompt}{_batch_camera_suffix(batch_camera_mode)}",
                            "project_id": project_id,
                            "aspect_ratio": node.data.get("aspectRatio") or batch_video_aspect_ratio,
                            "paygate_tier": paygate_tier,
                            "video_quality": node.data.get("videoQuality") or video_quality or "fast",
                        }
                        if len(start_media_ids) > 1:
                            params["start_media_ids"] = start_media_ids
                        else:
                            params["start_media_id"] = start_media_ids[0]
                        req_type = "gen_video"
                    
                node.status = "queued"
                session.add(node)
                session.commit()
                
                req = Request(
                    node_id=nid,
                    type=req_type,
                    params=params,
                    status="queued",
                )
                session.add(req)
                session.commit()
                session.refresh(req)
                req_id = req.id
                
            # Await request outside DB session lock
            get_worker().enqueue(req_id)
            settled = await _await_request(req_id)
            
            with get_session() as session:
                node = session.get(Node, nid)
                if not node:
                    continue
                if settled.status == "done":
                    result = settled.result or {}
                    media_ids = result.get("media_ids") or []
                    media_id = None
                    for m in media_ids:
                        if m:
                            media_id = m
                            break
                    node.status = "done"
                    node_data = dict(node.data)
                    node_data["mediaId"] = media_id
                    node_data["mediaIds"] = media_ids
                    node_data["renderedAt"] = datetime.now(timezone.utc).isoformat()
                    node_data.pop("error", None) # clear old error
                    node_data.pop("errorHint", None) # clear old friendly error
                    node.data = node_data
                    session.add(node)
                    session.commit()
                    node_map[nid] = node
                else:
                    failed_nodes.add(nid)
                    node.status = "error"
                    error_message = settled.error or "generation failed"
                    node.data = {
                        **dict(node.data),
                        "error": error_message,
                        "errorHint": _friendly_generation_error(error_message),
                    }
                    session.add(node)
                    session.commit()
        
        # Auto-assemble at the end of the batch if requested and all nodes succeeded
        if auto_assemble and not failed_nodes:
            logger.info(f"Auto-assembling videos for assembly node {assembly_node_id}")
            with get_session() as session:
                assembly_node = session.get(Node, assembly_node_id)
                data = (assembly_node.data or {}) if assembly_node else {}
                video_order = data.get("videoOrder") or []
                audio_media_id = data.get("audioMediaId")
            try:
                await _assemble_videos_impl(assembly_node_id, video_order, audio_media_id)
            except Exception as ae:
                logger.error(f"Auto-assembly failed for node {assembly_node_id}: {ae}")
                    
    except Exception as e:
        logger.error(f"Error in batch generation: {e}", exc_info=True)


@router.post("/node/{node_id}/generate-all")
async def generate_all_nodes(
    node_id: int,
    req: GenerateAllRequest,
    background_tasks: BackgroundTasks
):
    """Trigger background batch generation for all unrendered upstream nodes topologically."""
    with get_session() as session:
        node = session.get(Node, node_id)
        if not node:
            raise HTTPException(status_code=404, detail="Node not found")
        if node.type != "video_assembly":
            raise HTTPException(status_code=400, detail="Node must be of type 'video_assembly'")
            
        board_id = node.board_id
        project_mapping = session.get(BoardFlowProject, board_id)
        if not project_mapping:
            raise HTTPException(
                status_code=400,
                detail="Flow project not initialized for this board. Please open Flow first."
            )
            
        project_id = project_mapping.flow_project_id
        from flowboard.services.flow_client import flow_client
        paygate_tier = req.paygate_tier or flow_client.paygate_tier or "PAYGATE_TIER_ONE"
        
    background_tasks.add_task(
        run_batch_generation,
        node_id,
        project_id,
        paygate_tier,
        req.image_model,
        req.video_quality,
        req.video_model,
        req.omni_flash_duration,
        req.batch_video_aspect_ratio,
        req.batch_camera_mode,
        req.retry_failed,
        req.auto_assemble,
    )
    return {"ok": True, "message": "Batch generation started"}

