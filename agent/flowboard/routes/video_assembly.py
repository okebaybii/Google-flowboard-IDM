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


def detect_audio_beats(audio_path: str, video_duration: float) -> list[float]:
    """Detect beat times in an audio file using amplitude envelope analysis."""
    from moviepy import AudioFileClip
    import numpy as np

    try:
        audio = AudioFileClip(audio_path)
        # Read sound array at 100 samples per second (10ms bins)
        fps = 100
        sound_array = audio.to_soundarray(fps=fps)
        audio.close()

        if sound_array.size == 0:
            return []

        # Convert to mono by averaging channels
        if len(sound_array.shape) > 1:
            mono = np.mean(np.abs(sound_array), axis=1)
        else:
            mono = np.abs(sound_array)

        # Compute energy envelope with a rolling window (0.1s = 10 samples)
        window_size = 10
        envelope = np.convolve(mono, np.ones(window_size)/window_size, mode='same')

        # Detect local peaks (onsets) that are higher than the local average
        beats = []
        threshold_ratio = 1.3
        min_beat_distance_s = 0.4  # Minimum distance between beats (max 150 BPM)
        min_beat_distance_samples = int(min_beat_distance_s * fps)

        last_beat_idx = -min_beat_distance_samples

        for i in range(10, len(envelope) - 10):
            local_avg = np.mean(envelope[max(0, i-50):min(len(envelope), i+50)])
            if local_avg == 0:
                local_avg = 1e-5

            if (envelope[i] > envelope[i-1] and envelope[i] > envelope[i+1]
                and envelope[i] > local_avg * threshold_ratio
                and i - last_beat_idx >= min_beat_distance_samples):

                beat_time = i / fps
                if beat_time < video_duration:
                    beats.append(beat_time)
                    last_beat_idx = i

        logger.info(f"Beat-matching: detected {len(beats)} beats in background music.")
        return beats
    except Exception as e:
        logger.warning(f"Failed to detect audio beats: {e}")
        return []


def _run_moviepy_assembly(
    video_paths: list[str],
    narrations: list[str],
    audio_path: Optional[str],
    output_path: str,
    node_id: Optional[int] = None,
    aspect_ratio: str = "16:9"
):
    """Concatenate videos and overlay audio + dynamic TTS narration using MoviePy.
    
    This function executes in a separate thread to prevent blocking the async loop.
    """
    from moviepy import VideoFileClip, concatenate_videoclips, AudioFileClip, CompositeAudioClip, ImageClip
    from moviepy.video.fx import Resize, Crop, MultiplySpeed, CrossFadeIn
    from gtts import gTTS
    import os

    if aspect_ratio == "9:16":
        target_w, target_h = 720, 1280
    else:
        target_w, target_h = 1280, 720

    clips = []
    tts_audio_clips = []
    temp_files = []
    
    CHUNK_SIZE = 10
    total_clips = len(video_paths)
    
    try:
        if total_clips <= CHUNK_SIZE:
            # --- BẮT ĐẦU: LOGIC GỐC CHO VIDEO NGẮN ---
            raw_clips = []
            for path in video_paths:
                c = VideoFileClip(path)
                if c.w != target_w or c.h != target_h:
                    scale = max(target_w / c.w, target_h / c.h)
                    new_w = int(round(c.w * scale))
                    new_h = int(round(c.h * scale))
                    if new_w % 2 != 0:
                        new_w += 1
                    if new_h % 2 != 0:
                        new_h += 1
                    c = c.with_effects([
                        Resize(new_size=(new_w, new_h)),
                        Crop(x_center=new_w/2, y_center=new_h/2, width=target_w, height=target_h)
                    ])
                    logger.info(f"Aspect Ratio Alignment: Resized and center-cropped clip {path} to target {target_w}x{target_h}")
                raw_clips.append(c)

            # 1. Detect audio beats if bg music exists
            total_raw_duration = sum(c.duration for c in raw_clips)
            beats = detect_audio_beats(audio_path, total_raw_duration) if audio_path else []

            # 2. Load video clips, align durations, and generate aligned TTS narration audio
            cumulative_time = 0.0
            for i, clip in enumerate(raw_clips):
                natural_duration = clip.duration
                target_end_time = cumulative_time + natural_duration
                
                # Align end of clip to closest music beat (except the last clip)
                if i < len(raw_clips) - 1 and beats:
                    closest_beat = min(beats, key=lambda b: abs(b - target_end_time))
                    if abs(closest_beat - target_end_time) <= 1.0:
                        new_duration = closest_beat - cumulative_time
                        if new_duration > 0.5:
                            clip = clip.subclipped(0, new_duration)
                            logger.info(f"Beat-matching: aligned scene {i+1} duration from {natural_duration:.2f}s to {new_duration:.2f}s (beat at {closest_beat:.2f}s)")
                
                # Generate TTS narration audio for this scene
                narration_text = narrations[i] if i < len(narrations) else ""
                tts_duration = 0.0
                tts_clip = None
                if narration_text and narration_text.strip():
                    try:
                        temp_tts_path = f"temp_tts_{i}_{os.getpid()}.mp3"
                        try:
                            import edge_tts
                            import asyncio
                            async def _gen_edge_tts():
                                communicate = edge_tts.Communicate(narration_text.strip(), "vi-VN-HoaiMyNeural")
                                await communicate.save(temp_tts_path)
                            import time
                            max_retries = 3
                            for attempt in range(max_retries):
                                try:
                                    asyncio.run(_gen_edge_tts())
                                    logger.info(f"Edge TTS: Generated emotional neural voiceover for scene {i+1} on attempt {attempt + 1}")
                                    break
                                except Exception as edge_retry_err:
                                    if attempt < max_retries - 1:
                                        logger.warning(f"Edge TTS attempt {attempt + 1} failed: {edge_retry_err}. Retrying...")
                                        time.sleep(1.5)
                                    else:
                                        raise edge_retry_err
                        except Exception as edge_err:
                            logger.warning(f"Edge TTS failed after {max_retries} attempts, falling back to gTTS: {edge_err}")
                            tts = gTTS(text=narration_text.strip(), lang="vi")
                            tts.save(temp_tts_path)
                            
                        temp_files.append(temp_tts_path)
                        tts_clip = AudioFileClip(temp_tts_path)
                        tts_duration = tts_clip.duration
                    except Exception as tts_err:
                        logger.error(f"Failed to generate TTS for scene {i}: {tts_err}")
                
                # Dynamic speed stretching or hybrid freeze-frame to sync frames to narration
                if tts_duration > clip.duration:
                    factor = clip.duration / tts_duration
                    if factor >= 0.5:
                        logger.info(f"Speed Stretching: slow down scene {i+1} from {clip.duration:.2f}s to {tts_duration:.2f}s (factor {factor:.2f})")
                        try:
                            clip = clip.with_effects([MultiplySpeed(factor)])
                        except Exception as speed_err:
                            logger.error(f"Failed to apply speed stretching: {speed_err}")
                    else:
                        logger.info(f"Speed Hybrid: slow down to 50% speed and freeze last frame.")
                        try:
                            clip = clip.with_effects([MultiplySpeed(0.5)])
                            freeze_duration = tts_duration - clip.duration
                            last_frame = clip.get_frame(clip.duration - 0.05)
                            fps = clip.fps or 24
                            freeze_clip = ImageClip(last_frame).with_duration(freeze_duration).with_fps(fps)
                            clip = concatenate_videoclips([clip, freeze_clip])
                        except Exception as hybrid_err:
                            logger.error(f"Failed to apply hybrid speed/freeze effect: {hybrid_err}")
                
                clips.append(clip)
                if tts_clip:
                    tts_clip = tts_clip.with_start(cumulative_time)
                    tts_audio_clips.append(tts_clip)
                        
                cumulative_time += clip.duration - 0.5
            
            clips_with_fade = [clips[0]]
            for c in clips[1:]:
                clips_with_fade.append(c.with_effects([CrossFadeIn(0.5)]))
                
            final_clip = concatenate_videoclips(clips_with_fade, padding=-0.5, method="compose")
            video_duration = final_clip.duration
            
            audio_components = []
            if audio_path:
                bg_music = AudioFileClip(audio_path)
                if bg_music.duration > video_duration:
                    bg_music = bg_music.subclipped(0, video_duration)
                else:
                    bg_music = bg_music.with_duration(video_duration)
                bg_music = bg_music.volumex(0.2)
                audio_components.append(bg_music)
                
            if tts_audio_clips:
                audio_components.extend(tts_audio_clips)
                
            if audio_components:
                final_audio = CompositeAudioClip(audio_components)
                final_clip = final_clip.with_audio(final_audio)
                
            logger_obj = DBProgressBarLogger(node_id) if node_id is not None else None
            final_clip.write_videofile(
                output_path,
                codec="libx264",
                audio_codec="aac",
                temp_audiofile="temp-audio.m4a",
                remove_temp=True,
                logger=logger_obj
            )
            final_clip.close()
            # --- KẾT THÚC: LOGIC GỐC CHO VIDEO NGẮN ---
            return

        # ==============================================================================
        # --- BẮT ĐẦU: LOGIC CHUNKED ASSEMBLY CHO VIDEO RẤT DÀI (CHỐNG OOM) ---
        # ==============================================================================
        logger.info(f"Video contains {total_clips} scenes. Using Chunked Assembly (CHUNK_SIZE={CHUNK_SIZE}) to prevent OOM.")
        
        estimated_duration = total_clips * 5.0
        beats = detect_audio_beats(audio_path, estimated_duration) if audio_path else []
        
        chunk_files = []
        
        for chunk_idx in range(0, total_clips, CHUNK_SIZE):
            chunk_paths = video_paths[chunk_idx : chunk_idx + CHUNK_SIZE]
            chunk_narrs = narrations[chunk_idx : chunk_idx + CHUNK_SIZE]
            
            chunk_clips = []
            chunk_tts_audio_clips = []
            cumulative_time = 0.0
            
            logger.info(f"--- Processing Chunk {chunk_idx//CHUNK_SIZE + 1} ({len(chunk_paths)} clips) ---")
            for i, path in enumerate(chunk_paths):
                global_i = chunk_idx + i
                c = VideoFileClip(path)
                
                # Resize
                if c.w != target_w or c.h != target_h:
                    scale = max(target_w / c.w, target_h / c.h)
                    new_w = int(round(c.w * scale))
                    new_h = int(round(c.h * scale))
                    if new_w % 2 != 0: new_w += 1
                    if new_h % 2 != 0: new_h += 1
                    c = c.with_effects([
                        Resize(new_size=(new_w, new_h)),
                        Crop(x_center=new_w/2, y_center=new_h/2, width=target_w, height=target_h)
                    ])
                    
                natural_duration = c.duration
                target_end_time = cumulative_time + natural_duration + (chunk_idx * 5.0)
                
                # Beat matching
                if global_i < total_clips - 1 and beats:
                    closest_beat = min(beats, key=lambda b: abs(b - target_end_time))
                    if abs(closest_beat - target_end_time) <= 1.0:
                        new_duration = closest_beat - (cumulative_time + (chunk_idx * 5.0))
                        if new_duration > 0.5:
                            c = c.subclipped(0, new_duration)
                            
                # TTS
                narration_text = chunk_narrs[i] if i < len(chunk_narrs) else ""
                tts_duration = 0.0
                tts_clip = None
                if narration_text.strip():
                    try:
                        temp_tts_path = f"temp_tts_{global_i}_{os.getpid()}.mp3"
                        try:
                            import edge_tts, asyncio, time
                            async def _gen_edge_tts():
                                communicate = edge_tts.Communicate(narration_text.strip(), "vi-VN-HoaiMyNeural")
                                await communicate.save(temp_tts_path)
                            for attempt in range(3):
                                try:
                                    asyncio.run(_gen_edge_tts())
                                    break
                                except Exception:
                                    if attempt < 2: time.sleep(1.5)
                        except Exception:
                            tts = gTTS(text=narration_text.strip(), lang="vi")
                            tts.save(temp_tts_path)
                            
                        temp_files.append(temp_tts_path)
                        tts_clip = AudioFileClip(temp_tts_path)
                        tts_duration = tts_clip.duration
                    except Exception as tts_err:
                        logger.error(f"Failed TTS for chunk {chunk_idx} scene {i}: {tts_err}")
                        
                # Speed stretch
                if tts_duration > c.duration:
                    factor = c.duration / tts_duration
                    if factor >= 0.5:
                        c = c.with_effects([MultiplySpeed(factor)])
                    else:
                        c = c.with_effects([MultiplySpeed(0.5)])
                        freeze_duration = tts_duration - c.duration
                        last_frame = c.get_frame(c.duration - 0.05)
                        fps = c.fps or 24
                        freeze_clip = ImageClip(last_frame).with_duration(freeze_duration).with_fps(fps)
                        c = concatenate_videoclips([c, freeze_clip])
                        
                chunk_clips.append(c)
                if tts_clip:
                    tts_clip = tts_clip.with_start(cumulative_time)
                    chunk_tts_audio_clips.append(tts_clip)
                    
                cumulative_time += c.duration - 0.5
                
            # Compose chunk
            clips_with_fade = [chunk_clips[0]]
            for c in chunk_clips[1:]:
                clips_with_fade.append(c.with_effects([CrossFadeIn(0.5)]))
                
            chunk_final = concatenate_videoclips(clips_with_fade, padding=-0.5, method="compose")
            
            if chunk_tts_audio_clips:
                chunk_audio = CompositeAudioClip(chunk_tts_audio_clips)
                chunk_final = chunk_final.with_audio(chunk_audio)
                
            chunk_out = f"temp_chunk_{chunk_idx}_{os.getpid()}.mp4"
            logger_obj = DBProgressBarLogger(node_id) if node_id is not None else None
            
            logger.info(f"Writing Chunk {chunk_idx//CHUNK_SIZE + 1} to {chunk_out}")
            chunk_final.write_videofile(
                chunk_out,
                codec="libx264",
                audio_codec="aac",
                temp_audiofile=f"temp-audio-chunk-{chunk_idx}.m4a",
                remove_temp=True,
                logger=logger_obj
            )
            
            chunk_files.append(chunk_out)
            temp_files.append(chunk_out)
            
            # Giải phóng RAM cho Cụm hiện tại
            chunk_final.close()
            for c in chunk_clips: c.close()
            for t in chunk_tts_audio_clips: t.close()
            logger.info(f"--- Cleared RAM for Chunk {chunk_idx//CHUNK_SIZE + 1} ---")
            
        # MASTER STITCH
        logger.info(f"All {len(chunk_files)} chunks created. Starting Master Stitch.")
        master_clips = [VideoFileClip(p) for p in chunk_files]
        master_with_fade = [master_clips[0]]
        for c in master_clips[1:]:
            master_with_fade.append(c.with_effects([CrossFadeIn(0.5)]))
            
        final_master = concatenate_videoclips(master_with_fade, padding=-0.5, method="compose")
        video_duration = final_master.duration
        
        if audio_path:
            bg_music = AudioFileClip(audio_path)
            if bg_music.duration > video_duration:
                bg_music = bg_music.subclipped(0, video_duration)
            else:
                bg_music = bg_music.with_duration(video_duration)
                
            bg_music = bg_music.volumex(0.2)
            
            if final_master.audio:
                final_audio = CompositeAudioClip([final_master.audio, bg_music])
                final_master = final_master.with_audio(final_audio)
            else:
                final_master = final_master.with_audio(bg_music)
                
        logger.info(f"Writing final master video to {output_path}")
        final_master.write_videofile(
            output_path,
            codec="libx264",
            audio_codec="aac",
            temp_audiofile="temp-audio-master.m4a",
            remove_temp=True,
            logger=logger_obj
        )
        
        final_master.close()
        for c in master_clips: c.close()
        logger.info("Master Stitch complete. Chunked Assembly finished successfully!")
        
    finally:
        # Cleanup temporary files (TTS mp3 and chunk mp4)
        for tf in temp_files:
            try:
                if os.path.exists(tf):
                    os.remove(tf)
            except Exception as cleanup_err:
                logger.error(f"Failed to cleanup temp file {tf}: {cleanup_err}")
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
                
            # Determine aspect ratio dynamically
            resolved_aspect_ratio = "16:9"
            raw_assembly_aspect = node.data.get("batchVideoAspectRatio") or node.data.get("aspectRatio")
            if raw_assembly_aspect:
                if "PORTRAIT" in raw_assembly_aspect or raw_assembly_aspect == "9:16":
                    resolved_aspect_ratio = "9:16"
                elif "LANDSCAPE" in raw_assembly_aspect or raw_assembly_aspect == "16:9":
                    resolved_aspect_ratio = "16:9"
                else:
                    resolved_aspect_ratio = raw_assembly_aspect
            elif video_nodes:
                raw_aspect = video_nodes[0].data.get("aspectRatio") or "16:9"
                if "PORTRAIT" in raw_aspect:
                    resolved_aspect_ratio = "9:16"
                elif "LANDSCAPE" in raw_aspect:
                    resolved_aspect_ratio = "16:9"
                else:
                    resolved_aspect_ratio = raw_aspect
                
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
            node_id,
            resolved_aspect_ratio
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
            node_data["aspectRatio"] = resolved_aspect_ratio
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
    session,
    incoming_edges: dict[int, list[Edge]],
    allowed_types: set[str],
) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for edge in incoming_edges.get(node_id, []):
        src = session.get(Node, edge.source_id)
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
    if camera_mode == "pan_right":
        return " Camera: pan right, smooth panning camera action to the right, showing rightside background details, cinematic."
    if camera_mode == "pan_left":
        return " Camera: pan left, smooth panning camera action to the left, showing leftside background details, cinematic."
    if camera_mode == "zoom_in":
        return " Camera: push in, smooth camera zoom-in toward the subject, dramatic focus, high cinematic quality."
    if camera_mode == "zoom_out":
        return " Camera: pull out, smooth camera zoom-out/dolly-back showing wider scope/background, cinematic."
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
            try:
                with get_session() as session:
                    node = session.get(Node, nid)
                    if not node:
                        continue
                    
                    # Skip if already done
                    aspect_mismatch = False
                    if batch_video_aspect_ratio:
                        if node.type in ("image", "Storyboard"):
                            target_aspect = "IMAGE_ASPECT_RATIO_PORTRAIT" if batch_video_aspect_ratio == "VIDEO_ASPECT_RATIO_PORTRAIT" else "IMAGE_ASPECT_RATIO_LANDSCAPE"
                            if node.data.get("aspectRatio") != target_aspect:
                                aspect_mismatch = True
                        else:
                            if node.data.get("aspectRatio") != batch_video_aspect_ratio:
                                aspect_mismatch = True

                    media_id = node.data.get("mediaId")
                    if node.status == "done" and media_id and not aspect_mismatch:
                        logger.info(f"Node {nid} is already done. Skipping.")
                        continue
                    if node.status == "error" and media_id and not retry_failed and not aspect_mismatch:
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
                    
                    # Thu thập text từ prompt, image, storyboard upstream để đồng bộ
                    # context xuyên suốt pipeline (khung cảnh/scenery)
                    image_aspect_ratio = None
                    for pe in parent_edges:
                        pn = session.get(Node, pe.source_id)
                        if pn and pn.type in ("prompt", "image", "Storyboard"):
                            pn_text = pn.data.get("prompt", "").strip()
                            if pn.type in ("image", "Storyboard") and node.type in ("image", "Storyboard"):
                                pass  # Do not inherit previous image prompt to prevent exponential growth
                            else:
                                if pn_text and pn_text not in final_prompt:
                                    final_prompt += f". {pn_text}"
                            if pn.type in ("image", "Storyboard"):
                                image_aspect_ratio = pn.data.get("aspectRatio")
                    
                    if style_node:
                        style_id = style_node.data.get("activeStyleId", "hollywood")
                        suffix = STYLE_PROMPTS.get(style_id, "")
                        if suffix:
                            final_prompt = f"{final_prompt}{suffix}"
                    
                    # Build dispatch params. Match the manual generation path as
                    # closely as possible so batch output keeps the same character,
                    # visual asset, style, and pinned variant context.
                    ref_source_types = {"character", "image", "visual_asset", "Storyboard"}
                    
                    img_aspect = "IMAGE_ASPECT_RATIO_LANDSCAPE"
                    vid_aspect = "VIDEO_ASPECT_RATIO_LANDSCAPE"
                    
                    if node.type in ("image", "Storyboard"):
                        upstream_refs = _collect_reference_media_ids(
                            nid,
                            session,
                            incoming,
                            ref_source_types,
                        )

                        # Map batch_video_aspect_ratio to corresponding image aspect_ratio
                        img_aspect = node.data.get("aspectRatio")
                        if batch_video_aspect_ratio:
                            if batch_video_aspect_ratio == "VIDEO_ASPECT_RATIO_PORTRAIT":
                                img_aspect = "IMAGE_ASPECT_RATIO_PORTRAIT"
                            elif batch_video_aspect_ratio == "VIDEO_ASPECT_RATIO_LANDSCAPE":
                                img_aspect = "IMAGE_ASPECT_RATIO_LANDSCAPE"
                        if not img_aspect:
                            img_aspect = "IMAGE_ASPECT_RATIO_LANDSCAPE"

                        params = {
                            "prompt": final_prompt,
                            "project_id": project_id,
                            "aspect_ratio": img_aspect,
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
                        
                        vid_aspect = node.data.get("aspectRatio") or batch_video_aspect_ratio
                        if batch_video_aspect_ratio:
                            vid_aspect = batch_video_aspect_ratio
                        elif image_aspect_ratio:
                            if image_aspect_ratio == "IMAGE_ASPECT_RATIO_PORTRAIT":
                                vid_aspect = "VIDEO_ASPECT_RATIO_PORTRAIT"
                            elif image_aspect_ratio == "IMAGE_ASPECT_RATIO_LANDSCAPE":
                                vid_aspect = "VIDEO_ASPECT_RATIO_LANDSCAPE"

                        if is_omni:
                            ref_media_ids = _collect_reference_media_ids(
                                nid,
                                session,
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
                                "aspect_ratio": vid_aspect,
                                "paygate_tier": paygate_tier,
                            }
                            req_type = "gen_video_omni"
                        else:
                            start_media_ids = []
                            seen_start_ids = set()
                            for edge in parent_edges:
                                p_node = session.get(Node, edge.source_id)
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

                            # Thu thập ref_media_ids từ character/visual_asset
                            # để Veo i2v giữ đúng nhân vật qua liên kết
                            veo_ref_ids = _collect_reference_media_ids(
                                nid,
                                session,
                                incoming,
                                ref_source_types,
                            )

                            params = {
                                "prompt": f"{final_prompt}{_batch_camera_suffix(batch_camera_mode)}",
                                "project_id": project_id,
                                "aspect_ratio": vid_aspect,
                                "paygate_tier": paygate_tier,
                                "video_quality": node.data.get("videoQuality") or video_quality or "fast",
                            }
                            if veo_ref_ids:
                                params["ref_media_ids"] = veo_ref_ids
                            if len(start_media_ids) > 1:
                                params["start_media_ids"] = start_media_ids
                            else:
                                params["start_media_id"] = start_media_ids[0]
                            req_type = "gen_video"

                # We try generating the node with auto-retry up to 2 attempts
                max_attempts = 2
                settled = None
                error_message = "generation failed"
                success = False

                for attempt_idx in range(max_attempts):
                    logger.info(f"Node {nid} generation attempt {attempt_idx + 1}/{max_attempts}")
                    with get_session() as session:
                        node = session.get(Node, nid)
                        if not node:
                            break
                        node.status = "queued"
                        node_data = dict(node.data)
                        if node.type in ("image", "Storyboard"):
                            node_data["aspectRatio"] = img_aspect
                        else:
                            node_data["aspectRatio"] = vid_aspect
                        node.data = node_data
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
                    try:
                        settled = await _await_request(req_id)
                        if settled.status == "done":
                            success = True
                            break
                        else:
                            error_message = settled.error or "generation failed"
                            logger.warning(f"Node {nid} attempt {attempt_idx + 1} failed: {error_message}")
                    except Exception as ex:
                        error_message = str(ex)
                        logger.warning(f"Node {nid} attempt {attempt_idx + 1} raised error: {ex}")
                    
                    if attempt_idx < max_attempts - 1:
                        # Brief sleep between retries
                        await asyncio.sleep(2.0)

                with get_session() as session:
                    node = session.get(Node, nid)
                    if not node:
                        continue
                    if success and settled and settled.status == "done":
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
                        node.data = {
                            **dict(node.data),
                            "error": error_message,
                            "errorHint": _friendly_generation_error(error_message),
                        }
                        session.add(node)
                        session.commit()

            except Exception as loop_err:
                logger.error(f"Failed during batch processing of node {nid}: {loop_err}", exc_info=True)
                failed_nodes.add(nid)
                try:
                    with get_session() as session:
                        node = session.get(Node, nid)
                        if node:
                            node.status = "error"
                            node.data = {
                                **dict(node.data),
                                "error": str(loop_err),
                                "errorHint": _friendly_generation_error(str(loop_err)),
                            }
                            session.add(node)
                            session.commit()
                except Exception as db_err:
                    logger.error(f"Could not write failure status for node {nid} to DB: {db_err}")
        
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
                with get_session() as session:
                    assembly_node = session.get(Node, assembly_node_id)
                    if assembly_node:
                        assembly_node.status = "error"
                        assembly_node.data = {**dict(assembly_node.data), "error": str(ae)}
                        session.add(assembly_node)
                        session.commit()
        else:
            with get_session() as session:
                assembly_node = session.get(Node, assembly_node_id)
                if assembly_node:
                    if failed_nodes:
                        assembly_node.status = "error"
                        assembly_node.data = {**dict(assembly_node.data), "error": f"{len(failed_nodes)} upstream nodes failed."}
                    else:
                        assembly_node.status = "done"
                    session.add(assembly_node)
                    session.commit()
                    
    except Exception as e:
        logger.error(f"Error in batch generation: {e}", exc_info=True)
        with get_session() as session:
            assembly_node = session.get(Node, assembly_node_id)
            if assembly_node:
                assembly_node.status = "error"
                assembly_node.data = {**dict(assembly_node.data), "error": str(e)}
                session.add(assembly_node)
                session.commit()


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
        
        # Tự động khởi tạo Flow project nếu board chưa liên kết
        if not project_mapping:
            from flowboard.db.models import Board
            board = session.get(Board, board_id)
            if not board:
                raise HTTPException(status_code=404, detail="Board not found")
                
            from flowboard.services.flow_sdk import get_flow_sdk
            sdk = get_flow_sdk()
            resp = await sdk.create_project(title=board.name or "Untitled")
            if resp.get("error"):
                raise HTTPException(status_code=502, detail=f"Lỗi tạo Flow project: {resp['error']}")
            
            project_id = resp.get("project_id")
            if not project_id:
                raise HTTPException(status_code=502, detail="Invalid project_id returned from Flow SDK")
                
            project_mapping = BoardFlowProject(board_id=board_id, flow_project_id=project_id)
            session.add(project_mapping)
            session.commit()
            
        project_id = project_mapping.flow_project_id
        from flowboard.services.flow_client import flow_client
        paygate_tier = req.paygate_tier or flow_client.paygate_tier or "PAYGATE_TIER_ONE"
        
        node.status = "queued"
        session.add(node)
        session.commit()
        
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

