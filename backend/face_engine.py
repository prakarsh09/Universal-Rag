import os
import cv2
import base64
import logging
import threading
import numpy as np
from pathlib import Path
from typing import List, Dict, Any, Optional, Union
from PIL import Image

logger = logging.getLogger("face_engine")
logger.setLevel(logging.INFO)
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("[FaceEngine] %(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(ch)


class FaceEngine:
    """Singleton face analysis engine utilizing InsightFace's buffalo_l model.
    Performs SCRFD face detection, facial landmark alignment, and 512-dim ArcFace embedding extraction.
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, model_name: str = "buffalo_l", det_size: tuple = (640, 640)):
        if self._initialized:
            return

        with self._lock:
            if self._initialized:
                return

            logger.info(f"Initializing FaceEngine with model '{model_name}'...")
            try:
                from insightface.app import FaceAnalysis
                self.app = FaceAnalysis(name=model_name, providers=['CPUExecutionProvider'])
                self.app.prepare(ctx_id=-1, det_size=det_size)
                self.det_size = det_size
                self._initialized = True
                logger.info(f"FaceEngine successfully initialized.")
            except Exception as e:
                logger.error(f"Failed to initialize FaceAnalysis: {e}")
                self.app = None
                self._initialized = True

    def _load_image(self, image_input: Union[str, Path, bytes, np.ndarray, Image.Image]) -> Optional[np.ndarray]:
        """Loads and converts various input image representations into BGR numpy uint8 array."""
        try:
            if isinstance(image_input, (str, Path)):
                img_path = str(image_input)
                if not os.path.exists(img_path):
                    logger.error(f"Image path does not exist: {img_path}")
                    return None
                # Read using cv2 (supports Unicode paths on Windows via np.fromfile)
                data = np.fromfile(img_path, dtype=np.uint8)
                img = cv2.imdecode(data, cv2.IMREAD_COLOR)
                return img
            elif isinstance(image_input, bytes):
                data = np.frombuffer(image_input, dtype=np.uint8)
                return cv2.imdecode(data, cv2.IMREAD_COLOR)
            elif isinstance(image_input, Image.Image):
                rgb = np.array(image_input.convert("RGB"))
                return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            elif isinstance(image_input, np.ndarray):
                if image_input.ndim == 2:
                    return cv2.cvtColor(image_input, cv2.COLOR_GRAY2BGR)
                return image_input
            return None
        except Exception as e:
            logger.error(f"Error loading image in FaceEngine: {e}")
            return None

    def detect_and_embed(self, image_input: Union[str, Path, bytes, np.ndarray, Image.Image]) -> List[Dict[str, Any]]:
        """Detects all faces in the image, extracts 512-dim ArcFace identity vectors,
        bounding boxes, detection confidence, and base64 crop previews.
        
        Returns:
            List of dicts: [
                {
                    "index": int,
                    "bbox": [x1, y1, x2, y2],
                    "det_score": float,
                    "embedding": List[float] (512-d normalized),
                    "crop_base64": str (data URI),
                    "crop_image": np.ndarray (BGR crop)
                }, ...
            ]
        """
        if self.app is None:
            logger.error("FaceAnalysis app is not initialized.")
            return []

        img = self._load_image(image_input)
        if img is None:
            return []

        h, w = img.shape[:2]
        faces = self.app.get(img)
        if not faces:
            return []

        results = []
        for idx, face in enumerate(faces):
            # Bounding box: [x1, y1, x2, y2]
            raw_box = face.bbox.astype(int)
            x1 = max(0, min(w - 1, int(raw_box[0])))
            y1 = max(0, min(h - 1, int(raw_box[1])))
            x2 = max(x1 + 1, min(w, int(raw_box[2])))
            y2 = max(y1 + 1, min(h, int(raw_box[3])))
            bbox = [x1, y1, x2, y2]

            det_score = float(face.det_score)
            
            # Embedding is 512-dim, normalize to unit length if needed
            emb = face.embedding
            norm = np.linalg.norm(emb)
            if norm > 0:
                emb = emb / norm
            emb_list = [float(x) for x in emb]

            # Crop with slight padding for preview thumbnail
            pad_x = int((x2 - x1) * 0.15)
            pad_y = int((y2 - y1) * 0.15)
            crop_x1 = max(0, x1 - pad_x)
            crop_y1 = max(0, y1 - pad_y)
            crop_x2 = min(w, x2 + pad_x)
            crop_y2 = min(h, y2 + pad_y)

            crop_img = img[crop_y1:crop_y2, crop_x1:crop_x2]
            crop_b64 = ""
            if crop_img.size > 0:
                success, buffer = cv2.imencode(".jpg", crop_img, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
                if success:
                    crop_b64 = "data:image/jpeg;base64," + base64.b64encode(buffer).decode("utf-8")

            results.append({
                "index": idx,
                "bbox": bbox,
                "det_score": det_score,
                "embedding": emb_list,
                "crop_base64": crop_b64,
                "crop_image": crop_img
            })

        return results

    def save_face_crops(self, image_input: Union[str, Path, bytes, np.ndarray], faces: List[Dict[str, Any]], 
                        output_dir: Union[str, Path], file_prefix: str) -> List[str]:
        """Saves cropped aligned faces to disk and returns relative URLs."""
        os.makedirs(output_dir, exist_ok=True)
        crop_urls = []

        for face in faces:
            crop_img = face.get("crop_image")
            idx = face.get("index", 0)
            crop_filename = f"{file_prefix}_face_{idx}.jpg"
            dest_path = Path(output_dir) / crop_filename

            if crop_img is not None and crop_img.size > 0:
                # Save using cv2 imencode and tofile for windows path safety
                success, buffer = cv2.imencode(".jpg", crop_img, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
                if success:
                    with open(dest_path, "wb") as f:
                        f.write(buffer)
                crop_url = f"/images/_faces/{crop_filename}"
            else:
                crop_url = ""

            face["crop_url"] = crop_url
            crop_urls.append(crop_url)

        return crop_urls


# Global convenience accessor
_face_engine_instance: Optional[FaceEngine] = None

def get_face_engine() -> FaceEngine:
    global _face_engine_instance
    if _face_engine_instance is None:
        _face_engine_instance = FaceEngine()
    return _face_engine_instance
