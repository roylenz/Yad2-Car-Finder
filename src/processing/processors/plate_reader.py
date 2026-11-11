"""Find and read licence plates in a photo, locally (no network).

A small YOLO plate detector (open-image-models) finds plates; a plate OCR model (fast-plate-ocr)
reads each crop. Both are ONNX models that run on the Pi's CPU in well under a second per photo.
Readings that fit the Israeli 7/8-digit format are marked valid; `plate_registry` votes over
them per ad.
"""

import asyncio
import re
import threading

from processing.base import PhotoTarget, Processor

DETECTOR_MODEL = "yolo-v9-t-640-license-plate-end2end"
OCR_MODEL = "cct-s-v2-global-model"


class PlateReaderProcessor(Processor):
    name = "plate_reader"
    version = "1"
    target = "photo"

    _models = None
    _lock = threading.Lock()

    @staticmethod
    def available() -> str | None:
        try:
            import cv2  # noqa: F401
            import fast_plate_ocr  # noqa: F401
            import open_image_models  # noqa: F401
        except ImportError as error:
            return f"plate libraries not installed ({error.name})"
        return None

    @classmethod
    def models(cls):
        with cls._lock:
            if cls._models is None:
                from fast_plate_ocr import LicensePlateRecognizer
                from open_image_models import create_detector

                cls._models = (
                    create_detector(DETECTOR_MODEL, conf_thresh=0.25),
                    LicensePlateRecognizer(OCR_MODEL, device="cpu"),
                )
            return cls._models

    async def run(self, target: PhotoTarget) -> dict:
        return await asyncio.to_thread(self._read, target)

    def _read(self, target: PhotoTarget) -> dict:
        import cv2
        import numpy as np

        image = cv2.imread(str(target.path))
        if image is None:
            raise ValueError("Photo could not be decoded")
        detector, ocr = self.models()
        plates = []
        for found in detector.predict(image):
            box = found.bounding_box
            x1, y1, x2, y2 = (int(value) for value in (box.x1, box.y1, box.x2, box.y2))
            pad_x, pad_y = int((x2 - x1) * 0.08), int((y2 - y1) * 0.15)
            crop = image[max(y1 - pad_y, 0) : y2 + pad_y, max(x1 - pad_x, 0) : x2 + pad_x]
            if crop.size == 0:
                continue
            prediction = ocr.run(crop, return_confidence=True)[0]
            text = prediction.plate.strip("_ ")
            digits = re.sub(r"\D", "", text)
            probabilities = (
                prediction.char_probs if prediction.char_probs is not None else np.array([0.0])
            )
            plates.append(
                {
                    "box": [x1, y1, x2, y2],
                    "det_conf": round(float(found.confidence), 3),
                    "raw": prediction.plate,
                    "digits": digits,
                    "ocr_conf": round(float(np.mean(probabilities)), 3),
                    "min_char_conf": round(float(np.min(probabilities)), 3),
                    # Israeli plates are digits only: 7 (NN-NNN-NN) or 8 (NNN-NN-NNN).
                    "valid": len(digits) in (7, 8) and len(digits) == len(text),
                }
            )
        return {"plates": plates}
