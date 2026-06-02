import os
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Cache models
_app = None
_swapper = None

def get_face_swap_models():
    """Load FaceAnalysis and Inswapper ONNX models if insightface is installed."""
    global _app, _swapper
    if _app is not None and _swapper is not None:
        return _app, _swapper
        
    try:
        import insightface
        import onnxruntime
        
        # Initialize FaceAnalysis
        _app = insightface.app.FaceAnalysis(name="buffalo_l")
        _app.prepare(ctx_id=-1, det_size=(640, 640))  # -1 for CPU fallback
        
        # Path to inswapper model
        model_path = Path(os.path.expanduser("~/.insightface/models/inswapper_128.onnx"))
        if not model_path.exists():
            logger.warning("inswapper_128.onnx not found at ~/.insightface/models/inswapper_128.onnx")
            return None, None
            
        _swapper = insightface.model_zoo.get_model(str(model_path), download=False)
        return _app, _swapper
    except Exception as e:
        logger.warning(f"InsightFace is not fully configured for local face swapping: {e}")
        return None, None


def swap_faces_in_image(char_img_path: str, target_img_path: str, output_path: str) -> bool:
    """Swap face from character image to target image and save result."""
    app, swapper = get_face_swap_models()
    if not app or not swapper:
        return False
        
    try:
        import cv2
        src_img = cv2.imread(char_img_path)
        tgt_img = cv2.imread(target_img_path)
        if src_img is None or tgt_img is None:
            return False
            
        src_faces = app.get(src_img)
        if not src_faces:
            logger.warning("No face found in character reference image")
            return False
            
        tgt_faces = app.get(tgt_img)
        if not tgt_faces:
            logger.warning("No face found in target image")
            return False
            
        source_face = src_faces[0]
        result_img = tgt_img.copy()
        
        for face in tgt_faces:
            result_img = swapper.get(result_img, face, source_face, paste_back=True)
            
        cv2.imwrite(output_path, result_img)
        return True
    except Exception as e:
        logger.error(f"Error swapping faces in image: {e}")
        return False


def swap_faces_in_video(char_img_path: str, video_path: str, output_path: str) -> bool:
    """Perform frame-by-frame face swap on video clip using MoviePy."""
    app, swapper = get_face_swap_models()
    if not app or not swapper:
        return False
        
    try:
        from moviepy import VideoFileClip
        import cv2
        
        char_img = cv2.imread(char_img_path)
        if char_img is None:
            return False
            
        src_faces = app.get(char_img)
        if not src_faces:
            logger.warning("No face found in character image. Skipping face swap.")
            return False
        source_face = src_faces[0]
        
        def process_frame(frame):
            # MoviePy uses RGB, CV2 uses BGR
            bgr_frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
            faces = app.get(bgr_frame)
            if not faces:
                return frame
            
            result_frame = bgr_frame.copy()
            for face in faces:
                result_frame = swapper.get(result_frame, face, source_face, paste_back=True)
                
            return cv2.cvtColor(result_frame, cv2.COLOR_BGR2RGB)
            
        clip = VideoFileClip(video_path)
        new_clip = clip.fl_image(process_frame)
        new_clip.write_videofile(
            output_path,
            codec="libx264",
            audio_codec="aac",
            logger=None
        )
        clip.close()
        new_clip.close()
        return True
    except Exception as e:
        logger.error(f"Failed to face swap video: {e}")
        return False
