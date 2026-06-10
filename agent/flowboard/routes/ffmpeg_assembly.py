import os
import subprocess
import uuid
import asyncio
from pathlib import Path
from typing import Optional
import logging

logger = logging.getLogger(__name__)

async def _run_ffmpeg_assembly(
    video_paths: list[str],
    narrations: list[str],
    audio_path: Optional[str],
    output_path: str,
    node_id: Optional[int] = None,
    aspect_ratio: str = "16:9"
):
    from flowboard.services.tts import generate_speech
    
    temp_dir = Path("temp_assembly")
    temp_dir.mkdir(exist_ok=True)
    
    processed_clips = []
    
    for i, vpath in enumerate(video_paths):
        narr = narrations[i] if i < len(narrations) else ""
        clip_out = temp_dir / f"clip_{i}_{uuid.uuid4().hex}.mp4"
        
        if narr and narr.strip():
            tts_audio = temp_dir / f"tts_{i}_{uuid.uuid4().hex}.wav"
            await generate_speech(narr.strip(), str(tts_audio))
            
            cmd = [
                "ffmpeg", "-y", "-i", vpath, "-i", str(tts_audio),
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-map", "0:v:0", "-map", "1:a:0",
                "-shortest", str(clip_out)
            ]
            subprocess.run(cmd, check=True, capture_output=True)
            processed_clips.append(str(clip_out))
        else:
            cmd = [
                "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=24000",
                "-i", vpath,
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-map", "1:v:0", "-map", "0:a:0",
                "-shortest", str(clip_out)
            ]
            subprocess.run(cmd, check=True, capture_output=True)
            processed_clips.append(str(clip_out))
            
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
    subprocess.run(cmd, check=True, capture_output=True)
    
    if audio_path:
        cmd = [
            "ffmpeg", "-y", "-i", str(concat_out), "-i", audio_path,
            "-filter_complex", "[0:a]volume=1.0[narr];[1:a]volume=0.2[bg];[narr][bg]amix=inputs=2:duration=first[a]",
            "-map", "0:v", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            output_path
        ]
        subprocess.run(cmd, check=True, capture_output=True)
    else:
        import shutil
        shutil.move(str(concat_out), output_path)
