import os
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Cache models
_app = None
_swapper = None
# Once we know the face-swap stack can't load, remember it so we log the reason
# ONCE and then quietly skip on every subsequent generation instead of spamming
# the same error on each request.
_unavailable = False


def get_face_swap_models():
    """Load FaceAnalysis and Inswapper ONNX models if insightface is installed."""
    global _app, _swapper, _unavailable
    if _app is not None and _swapper is not None:
        return _app, _swapper
    if _unavailable:
        return None, None

    try:
        import onnxruntime  # noqa: F401
    except Exception as e:
        _unavailable = True
        # Most common cause on Windows: onnxruntime's native DLL fails to load
        # because the Visual C++ Redistributable is missing, or the Python
        # version is too new for the installed onnxruntime wheel. Without this
        # the face swap SILENTLY does nothing — surface it loudly so the user
        # knows the face shown is whatever Flow generated, not a real swap.
        logger.error(
            "Face swap DISABLED: onnxruntime failed to import (%s). "
            "Install the Microsoft Visual C++ Redistributable (x64) and use a "
            "Python version supported by onnxruntime (3.10-3.12). Until fixed, "
            "Character faces are NOT swapped onto generated media.",
            e,
        )
        return None, None

    try:
        import insightface

        # Initialize FaceAnalysis
        _app = insightface.app.FaceAnalysis(name="buffalo_l")
        _app.prepare(ctx_id=-1, det_size=(640, 640))  # -1 for CPU fallback

        # Path to inswapper model
        model_path = Path(os.path.expanduser("~/.insightface/models/inswapper_128.onnx"))
        if not model_path.exists():
            _unavailable = True
            logger.error(
                "Face swap DISABLED: inswapper_128.onnx not found at "
                "~/.insightface/models/inswapper_128.onnx"
            )
            return None, None

        _swapper = insightface.model_zoo.get_model(str(model_path), download=False)
        return _app, _swapper
    except Exception as e:
        _unavailable = True
        logger.error("Face swap DISABLED: InsightFace failed to initialize: %s", e)
        return None, None


def _largest_face(faces):
    """Return the largest detected face by bbox area."""
    if not faces:
        return None
    return max(
        faces,
        key=lambda f: max(0, float(f.bbox[2] - f.bbox[0])) * max(0, float(f.bbox[3] - f.bbox[1])),
    )


def swap_faces_in_image(char_img_path: str, target_img_path: str, output_path: str) -> bool:
    """Swap the largest character face onto the largest target face."""
    app, swapper = get_face_swap_models()
    if not app or not swapper:
        return False
        
    try:
        import cv2
        src_img = cv2.imread(char_img_path)
        tgt_img = cv2.imread(target_img_path)
        if src_img is None or tgt_img is None:
            return False
            
        source_face = _largest_face(app.get(src_img))
        if source_face is None:
            logger.warning("No face found in character reference image")
            return False
            
        target_face = _largest_face(app.get(tgt_img))
        if target_face is None:
            logger.warning("No face found in target image")
            return False
            
        result_img = swapper.get(tgt_img.copy(), target_face, source_face, paste_back=True)
        cv2.imwrite(output_path, result_img)
        return True
    except Exception as e:
        logger.error(f"Error swapping faces in image: {e}")
        return False


def swap_faces_in_video(char_img_path: str, video_path: str, output_path: str) -> bool:
    """Perform frame-by-frame face swap on the largest face in each video frame."""
    app, swapper = get_face_swap_models()
    if not app or not swapper:
        return False
        
    try:
        from moviepy import VideoFileClip
        import cv2
        
        char_img = cv2.imread(char_img_path)
        if char_img is None:
            return False
            
        source_face = _largest_face(app.get(char_img))
        if source_face is None:
            logger.warning("No face found in character image. Skipping face swap.")
            return False
        
        def process_frame(frame):
            # MoviePy uses RGB, CV2 uses BGR
            bgr_frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
            target_face = _largest_face(app.get(bgr_frame))
            if target_face is None:
                return frame
            
            result_frame = swapper.get(bgr_frame.copy(), target_face, source_face, paste_back=True)
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
