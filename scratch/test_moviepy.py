import sys
from pathlib import Path
sys.path.append(r"c:\Users\Admin\Documents\Google-flowboard-IDM\agent")

from moviepy import VideoFileClip, concatenate_videoclips
from moviepy.video.fx import Resize, Crop
import os

# Find two mp4 files in storage/media to test
media_dir = r"c:\Users\Admin\Documents\Google-flowboard-IDM\storage\media"
mp4_files = [str(p) for p in Path(media_dir).glob("*.mp4") if p.stat().st_size > 0][:2]

if len(mp4_files) < 2:
    mp4_files = [str(p) for p in Path(media_dir).glob("*.mp4")]

print("Testing with files:", mp4_files)

# Target sizes for 9:16 vertical
target_w, target_h = 720, 1280

clips = []
try:
    for path in mp4_files[:2]:
        c = VideoFileClip(path)
        print(f"Original size: {c.w}x{c.h}")
        # Scale and crop to target_w, target_h
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
        print(f"Resized and cropped size: {c.w}x{c.h}")
        clips.append(c)
        
    print("Concatenating using method='chain'...")
    final_clip = concatenate_videoclips(clips, method="chain")
    print("Resulting clip duration:", final_clip.duration)
    
    output_path = "test_output_moviepy.mp4"
    print(f"Writing test output to {output_path}...")
    final_clip.write_videofile(
        output_path,
        codec="libx264",
        audio_codec="aac",
        logger=None
    )
    print("Write complete! File size:", os.path.getsize(output_path))
    
    # Cleanup
    final_clip.close()
    for c in clips:
        c.close()
    if os.path.exists(output_path):
        os.remove(output_path)
    print("Test passed successfully!")
except Exception as e:
    print("ERROR DURING TEST:", e)
    import traceback
    traceback.print_exc()
