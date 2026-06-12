import os
import subprocess
import uuid
import asyncio
from pathlib import Path
from typing import Optional
import logging

logger = logging.getLogger(__name__)

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
                await generate_speech(narr.strip(), str(tts_audio))
                
                cmd = [
                    "ffmpeg", "-y", "-i", vpath, "-i", str(tts_audio),
                    "-vf", vf_scale,
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-c:a", "aac", "-b:a", "192k",
                    "-map", "0:v:0", "-map", "1:a:0",
                    "-shortest", str(clip_out)
                ]
                await asyncio.to_thread(subprocess.run, cmd, check=True, capture_output=True)
            else:
                cmd = [
                    "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=24000",
                    "-i", vpath,
                    "-vf", vf_scale,
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-c:a", "aac", "-b:a", "192k",
                    "-map", "1:v:0", "-map", "0:a:0",
                    "-shortest", str(clip_out)
                ]
                await asyncio.to_thread(subprocess.run, cmd, check=True, capture_output=True)
                
            return str(clip_out)
        except subprocess.CalledProcessError as e:
            logger.error(f"FFmpeg error processing clip {i}: {e.stderr.decode('utf-8', errors='ignore')}")
            raise RuntimeError(f"Failed to process clip {i}") from e

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
            p_esc = str(p).replace("'", "'\\''")
            f.write(f"file '{p_esc}'\n")
            
    concat_out = temp_dir / f"concat_out_{uuid.uuid4().hex}.mp4"
    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        str(concat_out)
    ]
    try:
        await asyncio.to_thread(subprocess.run, cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        logger.error(f"FFmpeg concat error: {e.stderr.decode('utf-8', errors='ignore')}")
        raise RuntimeError("Failed to concatenate clips") from e
    
    if audio_path:
        cmd = [
            "ffmpeg", "-y", "-i", str(concat_out), "-i", audio_path,
            "-filter_complex", "[0:a]volume=1.0[narr];[1:a]volume=0.2[bg];[narr][bg]amix=inputs=2:duration=first[a]",
            "-map", "0:v", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            output_path
        ]
        try:
            await asyncio.to_thread(subprocess.run, cmd, check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            logger.error(f"FFmpeg mix error: {e.stderr.decode('utf-8', errors='ignore')}")
            raise RuntimeError("Failed to mix background audio") from e
    else:
        import shutil
        shutil.move(str(concat_out), output_path)
