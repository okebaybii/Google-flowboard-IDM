import sys
from moviepy import ColorClip
from moviepy.video.fx import MultiplySpeed

print("Testing MoviePy MultiplySpeed...")
try:
    # Create a 4-second clip
    clip = ColorClip(size=(640, 480), color=(0, 255, 0), duration=4.0)
    clip = clip.with_fps(24)
    print("Original duration:", clip.duration)
    
    # Slow down to 50% speed (makes duration 8.0 seconds)
    # The factor for MultiplySpeed in MoviePy 2.x is:
    # if speed is multiplied by factor, new duration = old_duration / factor
    # So factor = 0.5 means twice as slow (duration becomes 8.0)
    clip_slow = clip.with_effects([MultiplySpeed(0.5)])
    print("Slowed duration:", clip_slow.duration)
    
    # Speed up by 2.0 (makes duration 2.0 seconds)
    clip_fast = clip.with_effects([MultiplySpeed(2.0)])
    print("Fast duration:", clip_fast.duration)
    
    # Clean up
    clip.close()
    clip_slow.close()
    clip_fast.close()
    print("✅ MoviePy MultiplySpeed APIs verified successfully!")
except Exception as e:
    print("❌ MoviePy speed test failed:")
    import traceback
    traceback.print_exc()
