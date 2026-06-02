import sys
sys.path.append(r"c:\Users\Admin\Documents\Google-flowboard-IDM\agent")

from moviepy import VideoFileClip

media_ids = [
    "d98f4d3b-2cde-4759-8f00-cb07025ec5e5",
    "05ea2286-85a8-4fcf-b251-b48ef602f8a7",
    "f35c6933-e55f-4ac3-86a9-4048c0ab1614"
]

media_dir = r"c:\Users\Admin\Documents\Google-flowboard-IDM\storage\media"
for m in media_ids:
    path = f"{media_dir}\\{m}.mp4"
    try:
        c = VideoFileClip(path)
        print(f"File: {m}.mp4 | SIZE: {c.w}x{c.h}")
        c.close()
    except Exception as e:
        print(f"File: {m}.mp4 | Error: {e}")
