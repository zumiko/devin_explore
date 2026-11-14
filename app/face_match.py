"""Open-source face match between an ID document photo and a selfie.

Uses OpenCV's YuNet face detector and SFace face recognizer (models from
github.com/opencv/opencv_zoo). This stands in for the face-match step a KYC
vendor (Persona, Onfido, ...) would run; it has no liveness or document
authenticity checks.
"""
import os
import urllib.request

import cv2
import numpy as np

MODEL_DIR = os.path.join(os.path.dirname(__file__), "models")
MODEL_BASE = "https://github.com/opencv/opencv_zoo/raw/main/models"
DETECTOR = ("face_detection_yunet_2023mar.onnx", f"{MODEL_BASE}/face_detection_yunet/face_detection_yunet_2023mar.onnx")
RECOGNIZER = ("face_recognition_sface_2021dec.onnx",
              f"{MODEL_BASE}/face_recognition_sface/face_recognition_sface_2021dec.onnx")
MAX_SIDE = 1280

_detector = None
_recognizer = None


class FaceMatchError(Exception):
    pass


def _model_path(name, url):
    path = os.path.join(MODEL_DIR, name)
    if not os.path.exists(path):
        os.makedirs(MODEL_DIR, exist_ok=True)
        urllib.request.urlretrieve(url, path + ".part")
        os.replace(path + ".part", path)
    return path


def _models():
    global _detector, _recognizer
    if _detector is None:
        _detector = cv2.FaceDetectorYN.create(_model_path(*DETECTOR), "", (320, 320), 0.7, 0.3, 5000)
        _recognizer = cv2.FaceRecognizerSF.create(_model_path(*RECOGNIZER), "")
    return _detector, _recognizer


def _decode(data, label):
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise FaceMatchError(f"Couldn't read the {label} as an image. Use a JPG or PNG.")
    scale = MAX_SIDE / max(img.shape[:2])
    if scale < 1:
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    return img


def _largest_face(img, label):
    detector, recognizer = _models()
    detector.setInputSize((img.shape[1], img.shape[0]))
    _, faces = detector.detect(img)
    if faces is None or len(faces) == 0:
        raise FaceMatchError(f"No face found in the {label}.")
    face = max(faces, key=lambda f: f[2] * f[3])
    aligned = recognizer.alignCrop(img, face)
    return aligned, recognizer.feature(aligned), len(faces), float(face[-1])


def compare(id_bytes, selfie_bytes):
    """Return similarity (cosine, roughly -1..1; same person usually > 0.36) plus aligned face crops."""
    id_face, id_feat, id_count, id_conf = _largest_face(_decode(id_bytes, "ID photo"), "ID photo")
    selfie_face, selfie_feat, selfie_count, selfie_conf = _largest_face(_decode(selfie_bytes, "selfie"), "selfie")
    _, recognizer = _models()
    score = recognizer.match(id_feat, selfie_feat, cv2.FaceRecognizerSF_FR_COSINE)
    return {
        "score": round(float(score), 3),
        "id_face_jpg": cv2.imencode(".jpg", id_face)[1].tobytes(),
        "selfie_face_jpg": cv2.imencode(".jpg", selfie_face)[1].tobytes(),
        "id_faces_found": id_count,
        "selfie_faces_found": selfie_count,
        "id_face_confidence": round(id_conf, 2),
        "selfie_face_confidence": round(selfie_conf, 2),
    }
