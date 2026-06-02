import sys
sys.path.append(r"c:\Users\Admin\Documents\Google-flowboard-IDM\agent")

from moviepy import VideoFileClip

path = r"c:\Users\Admin\Documents\Google-flowboard-IDM\storage\media\e07c2380-4537-49ae-a145-8cce612c306f.mp4"
try:
    c = VideoFileClip(path)
    print(f"VIDEO DIMENSIONS: {c.w}x{c.h}")
    c.close()
except Exception as e:
    print("Error:", e)
