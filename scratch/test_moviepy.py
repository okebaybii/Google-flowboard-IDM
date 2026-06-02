import sys
from moviepy import VideoFileClip, ImageClip, concatenate_videoclips
import numpy as np

print("Testing MoviePy import and APIs...")
try:
    # Create a dummy 3-second video clip using numpy
    from moviepy import ColorClip
    clip = ColorClip(size=(640, 480), color=(255, 0, 0), duration=3.0)
    clip = clip.with_fps(24)
    print("Created ColorClip. Duration:", clip.duration)
    
    # Get last frame
    last_frame = clip.get_frame(clip.duration - 0.05)
    print("Got last frame. Shape:", last_frame.shape)
    
    # Create ImageClip from the frame
    img_clip = ImageClip(last_frame).with_duration(2.0)
    img_clip = img_clip.with_fps(24)
    print("Created ImageClip from frame. Duration:", img_clip.duration)
    
    # Concatenate
    final = concatenate_videoclips([clip, img_clip])
    print("Concatenation successful. Final duration:", final.duration)
    
    # Clean up
    clip.close()
    img_clip.close()
    final.close()
    print("✅ All MoviePy API calls succeeded!")
except Exception as e:
    print("❌ MoviePy test failed:")
    import traceback
    traceback.print_exc()
