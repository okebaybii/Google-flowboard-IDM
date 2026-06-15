import os
import subprocess
import uuid
import asyncio
import shutil
from pathlib import Path
from typing import Optional
import logging

logger = logging.getLogger(__name__)


def _resolve_ffmpeg() -> str:
    repo_root = Path(__file__).resolve().parents[3]
    local_ffmpeg = repo_root / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
    if local_ffmpeg.exists():
        return str(local_ffmpeg)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg
    raise RuntimeError(
        "FFmpeg not found. Run install-all.bat to install local FFmpeg, or install FFmpeg and add it to PATH, then restart the backend."
    )


async def _run_command(cmd: list[str], *, error_label: str) -> None:
    try:
        await asyncio.to_thread(subprocess.run, cmd, check=True, capture_output=True)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "FFmpeg not found. Run install-all.bat to install local FFmpeg, or install FFmpeg and add it to PATH, then restart the backend."
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="ignore") if exc.stderr else ""
        logger.error("%s: %s", error_label, stderr)
        detail = stderr.strip()[-1200:] if stderr.strip() else str(exc)
        raise RuntimeError(f"{error_label}: {detail}") from exc


async def _process_single_clip(i: int, vpath: str, narr: str, temp_dir: Path, aspect_ratio: str, sem: asyncio.Semaphore) -> str:
    from flowboard.services.tts import generate_speech
    
    async with sem:
        clip_out = temp_dir / f"clip_{i}_{uuid.uuid4().hex}.mp4"
        
        # Determine target resolution based on aspect ratio
        if aspect_ratio == "9:16":
            target_w, target_h = 720, 1280
        elif aspect_ratio == "1:1":
            target_w, target_h = 1024, 1024
        else:
            # Default to 16:9
            target_w, target_h = 1280, 720
            
        vf_scale = f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,setsar=1"
        
        try:
            if narr and narr.strip():
                tts_audio = temp_dir / f"tts_{i}_{uuid.uuid4().hex}.wav"
                try:
                    await generate_speech(narr.strip(), str(tts_audio))
                except Exception as exc:
                    logger.warning("TTS failed for clip %s; continuing without narration: %s", i, exc)
                    tts_audio = None

                if tts_audio is not None:
                    ffmpeg = _resolve_ffmpeg()
                    cmd = [
                        ffmpeg, "-y", "-i", vpath, "-i", str(tts_audio),
                        "-vf", vf_scale,
                        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                        "-c:a", "aac", "-b:a", "192k",
                        "-map", "0:v:0", "-map", "1:a:0",
                        "-shortest", str(clip_out)
                    ]
                    await _run_command(cmd, error_label=f"Failed to process clip {i}")
                else:
                    ffmpeg = _resolve_ffmpeg()
                    cmd = [
                        ffmpeg, "-y", "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=24000",
                        "-i", vpath,
                        "-vf", vf_scale,
                        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                        "-c:a", "aac", "-b:a", "192k",
                        "-map", "1:v:0", "-map", "0:a:0",
                        "-shortest", str(clip_out)
                    ]
                    await _run_command(cmd, error_label=f"Failed to process clip {i}")
            else:
                ffmpeg = _resolve_ffmpeg()
                cmd = [
                    ffmpeg, "-y", "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=24000",
                    "-i", vpath,
                    "-vf", vf_scale,
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-c:a", "aac", "-b:a", "192k",
                    "-map", "1:v:0", "-map", "0:a:0",
                    "-shortest", str(clip_out)
                ]
                await _run_command(cmd, error_label=f"Failed to process clip {i}")
                
            return str(clip_out)
        except RuntimeError:
            raise

async def _run_ffmpeg_assembly(
    video_paths: list[str],
    narrations: list[str],
    audio_path: Optional[str],
    output_path: str,
    node_id: Optional[int] = None,
    aspect_ratio: str = "16:9"
):
    temp_dir = Path("temp_assembly")
    temp_dir.mkdir(exist_ok=True)
    
    # Process max 3 clips concurrently to prevent CPU overload
    sem = asyncio.Semaphore(3)
    
    tasks = []
    for i, vpath in enumerate(video_paths):
        narr = narrations[i] if i < len(narrations) else ""
        tasks.append(_process_single_clip(i, vpath, narr, temp_dir, aspect_ratio, sem))
        
    processed_clips = await asyncio.gather(*tasks)
            
    concat_list = temp_dir / f"concat_{uuid.uuid4().hex}.txt"
    with open(concat_list, "w", encoding="utf-8") as f:
        for p in processed_clips:
            p_abs = Path(p).resolve().as_posix()
            p_esc = p_abs.replace("'", "'\\''")
            f.write(f"file '{p_esc}'\n")
            
    concat_out = temp_dir / f"concat_out_{uuid.uuid4().hex}.mp4"
    ffmpeg = _resolve_ffmpeg()
    cmd = [
        ffmpeg, "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        str(concat_out)
    ]
    await _run_command(cmd, error_label="Failed to concatenate clips")
    
    if audio_path:
        ffmpeg = _resolve_ffmpeg()
        cmd = [
            ffmpeg, "-y", "-i", str(concat_out), "-i", audio_path,
            "-filter_complex", "[0:a]volume=1.0[narr];[1:a]volume=0.2[bg];[narr][bg]amix=inputs=2:duration=first[a]",
            "-map", "0:v", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            output_path
        ]
        await _run_command(cmd, error_label="Failed to mix background audio")
    else:
        import shutil
        shutil.move(str(concat_out), output_path)
