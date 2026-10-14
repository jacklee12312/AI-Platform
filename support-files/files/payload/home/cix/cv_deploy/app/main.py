#!/usr/bin/env python3
"""Small offline HTTP service for CIX P1 NOE CV models.

The service intentionally keeps only one .cix model loaded at a time.  This
avoids competing NOE contexts and makes memory use predictable on the box.
"""

from __future__ import annotations

import base64
import io
import json
import math
import os
import signal
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np
from PIL import Image, UnidentifiedImageError
from NOE_Engine import EngineInfer


MODEL_ROOT = os.environ.get("MODEL_ROOT", "/models")
HOST = os.environ.get("CV_HOST", "0.0.0.0")
PORT = int(os.environ.get("CV_PORT", "17710"))
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(25 * 1024 * 1024)))


MODELS = {
    "mobilenet_v2": {
        "task": "image-classification", "size": (224, 224), "preprocess": "imagenet",
        "logical_outputs": [[1, 1000]],
    },
    "inception_v3": {
        "task": "image-classification", "size": (299, 299), "preprocess": "imagenet",
        "logical_outputs": [[1, 1000]],
    },
    "yolov11_l": {
        "task": "object-detection", "size": (640, 640), "preprocess": "yolo",
        "logical_outputs": [[1, 84, 8400]], "postprocess": "ultralytics",
    },
    "yolov11_n": {
        "task": "object-detection", "size": (640, 640), "preprocess": "yolo",
        "logical_outputs": [[1, 84, 8400]], "postprocess": "ultralytics",
    },
    "yolov11n_obb": {
        "task": "oriented-object-detection", "size": (640, 640), "preprocess": "yolo",
        "logical_outputs": [[1, 20, 8400]], "postprocess": "ultralytics-obb",
    },
    "yolov12_s": {
        "task": "object-detection", "size": (640, 640), "preprocess": "yolo",
        "logical_outputs": [[1, 84, 8400]], "postprocess": "ultralytics",
    },
    "yolov12_m": {
        "task": "object-detection", "size": (640, 640), "preprocess": "yolo",
        "logical_outputs": [[1, 84, 8400]], "postprocess": "ultralytics",
    },
    "retinaface": {
        "task": "face-detection", "size": (1024, 624), "preprocess": "retinaface",
        "logical_outputs": [[1, 26240, 4], [1, 26240, 2], [1, 26240, 10]],
        "postprocess": "retinaface",
    },
    "yolov5s_face": {
        "task": "face-detection", "size": (640, 640), "preprocess": "stretch-yolo",
        "logical_outputs": [[1, 48, 80, 80], [1, 48, 40, 40], [1, 48, 20, 20]],
        "postprocess": "yolov5-face", "as_list": True,
    },
    "scrfd": {
        "task": "face-detection", "size": (640, 640), "preprocess": "scrfd",
        "logical_outputs": [
            [12800, 1], [3200, 1], [800, 1], [12800, 4], [3200, 4],
            [800, 4], [12800, 10], [3200, 10], [800, 10],
        ],
        "postprocess": "scrfd",
    },
    "arcface": {
        "task": "face-embedding", "size": (112, 112), "preprocess": "arcface",
        "logical_outputs": [[1, 512]], "postprocess": "embedding",
    },
    "yolov8l_seg": {
        "task": "instance-segmentation", "size": (640, 640), "preprocess": "yolo",
        "logical_outputs": [[1, 116, 8400], [1, 32, 160, 160]],
        "postprocess": "yolov8-seg", "as_list": True,
    },
    "bytetrack_s": {
        "task": "object-tracking-detector", "size": (1088, 608), "preprocess": "bytetrack",
        "logical_outputs": [[1, 13566, 6]], "postprocess": "bytetrack",
    },
}


def _model_path(model_id: str) -> str:
    return os.path.join(MODEL_ROOT, model_id, "model.cix")


def _sigmoid(value: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(value, -30.0, 30.0)))


def _nms(boxes: np.ndarray, scores: np.ndarray, threshold: float, limit: int = 100) -> list[int]:
    if boxes.size == 0:
        return []
    x1, y1, x2, y2 = boxes.T
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size and len(keep) < limit:
        current = int(order[0])
        keep.append(current)
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(x1[current], x1[rest])
        yy1 = np.maximum(y1[current], y1[rest])
        xx2 = np.minimum(x2[current], x2[rest])
        yy2 = np.minimum(y2[current], y2[rest])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        union = areas[current] + areas[rest] - inter + 1e-7
        order = rest[inter / union <= threshold]
    return keep


def _prepare_image(data: bytes, spec: dict) -> tuple[np.ndarray, dict]:
    image = Image.open(io.BytesIO(data)).convert("RGB")
    original_w, original_h = image.size
    target_w, target_h = spec["size"]
    mode = spec["preprocess"]
    meta = {"original_size": [original_w, original_h], "target_size": [target_w, target_h]}

    if mode == "imagenet":
        array = np.asarray(image.resize((target_w, target_h), Image.Resampling.BILINEAR), dtype=np.float32) / 255.0
        array = (array - np.array([0.485, 0.456, 0.406], dtype=np.float32)) / np.array(
            [0.229, 0.224, 0.225], dtype=np.float32
        )
    elif mode in {"yolo", "bytetrack", "scrfd"}:
        ratio = min(target_w / original_w, target_h / original_h)
        resized_w = max(1, int(round(original_w * ratio)))
        resized_h = max(1, int(round(original_h * ratio)))
        resized = image.resize((resized_w, resized_h), Image.Resampling.BILINEAR)
        if mode in {"bytetrack", "scrfd"}:
            pad_x, pad_y = 0, 0
        else:
            pad_x = (target_w - resized_w) // 2
            pad_y = (target_h - resized_h) // 2
        canvas = Image.new("RGB", (target_w, target_h), (114, 114, 114) if mode != "scrfd" else (0, 0, 0))
        canvas.paste(resized, (pad_x, pad_y))
        array = np.asarray(canvas, dtype=np.float32)
        if mode == "scrfd":
            array = (array - 127.5) / 128.0
        else:
            array = array / 255.0
            if mode == "bytetrack":
                array = (array - np.array([0.485, 0.456, 0.406], dtype=np.float32)) / np.array(
                    [0.229, 0.224, 0.225], dtype=np.float32
                )
        meta.update({"ratio": ratio, "pad": [pad_x, pad_y]})
    elif mode == "retinaface":
        array = np.asarray(image.resize((target_w, target_h), Image.Resampling.BILINEAR), dtype=np.float32)
        array = array[:, :, ::-1] - np.array([104.0, 117.0, 123.0], dtype=np.float32)
        meta.update({"ratio": [target_w / original_w, target_h / original_h], "pad": [0, 0]})
    elif mode == "arcface":
        array = np.asarray(image.resize((target_w, target_h), Image.Resampling.BILINEAR), dtype=np.float32)
        array = (array - 127.5) / 127.5
    elif mode == "stretch-yolo":
        array = np.asarray(image.resize((target_w, target_h), Image.Resampling.BILINEAR), dtype=np.float32) / 255.0
        meta.update({"ratio": [target_w / original_w, target_h / original_h], "pad": [0, 0]})
    else:
        raise ValueError(f"unknown preprocess mode: {mode}")

    tensor = np.ascontiguousarray(array.transpose(2, 0, 1)[None, ...], dtype=np.float32)
    return tensor, meta


def _restore_xyxy(boxes: np.ndarray, meta: dict) -> np.ndarray:
    boxes = boxes.copy()
    ratio = meta.get("ratio", 1.0)
    pad_x, pad_y = meta.get("pad", [0, 0])
    if isinstance(ratio, list):
        boxes[:, [0, 2]] /= ratio[0]
        boxes[:, [1, 3]] /= ratio[1]
    else:
        boxes[:, [0, 2]] = (boxes[:, [0, 2]] - pad_x) / ratio
        boxes[:, [1, 3]] = (boxes[:, [1, 3]] - pad_y) / ratio
    original_w, original_h = meta["original_size"]
    boxes[:, [0, 2]] = boxes[:, [0, 2]].clip(0, original_w)
    boxes[:, [1, 3]] = boxes[:, [1, 3]].clip(0, original_h)
    return boxes


def _box_records(boxes: np.ndarray, scores: np.ndarray, classes: np.ndarray, keep: list[int], extra=None) -> list[dict]:
    records = []
    for index in keep:
        record = {
            "box": [round(float(v), 2) for v in boxes[index]],
            "score": round(float(scores[index]), 6),
            "class_id": int(classes[index]),
        }
        if extra is not None:
            record.update(extra(index))
        records.append(record)
    return records


def _post_ultralytics(outputs, spec, meta, confidence, iou):
    channels = spec["logical_outputs"][0][1]
    rows = np.asarray(outputs[0]).reshape(channels, -1).T
    post = spec["postprocess"]
    if post == "ultralytics-obb":
        class_scores = rows[:, 4:-1]
        angles = rows[:, -1]
    else:
        class_scores = rows[:, 4:84]
        angles = None
    classes = np.argmax(class_scores, axis=1)
    scores = class_scores[np.arange(class_scores.shape[0]), classes]
    selected = scores >= confidence
    rows, classes, scores = rows[selected], classes[selected], scores[selected]
    if angles is not None:
        angles = angles[selected]
    if not len(rows):
        return {"detections": [], "count": 0}
    boxes = np.column_stack((
        rows[:, 0] - rows[:, 2] / 2, rows[:, 1] - rows[:, 3] / 2,
        rows[:, 0] + rows[:, 2] / 2, rows[:, 1] + rows[:, 3] / 2,
    ))
    boxes = _restore_xyxy(boxes, meta)
    keep = _nms(boxes, scores, iou)
    extra = None if angles is None else lambda i: {"angle_rad": round(float(angles[i]), 6)}
    detections = _box_records(boxes, scores, classes, keep, extra)
    return {"detections": detections, "count": len(detections), "nms": "axis-aligned" if angles is not None else "standard"}


def _distance2bbox(points: np.ndarray, distance: np.ndarray) -> np.ndarray:
    return np.column_stack((
        points[:, 0] - distance[:, 0], points[:, 1] - distance[:, 1],
        points[:, 0] + distance[:, 2], points[:, 1] + distance[:, 3],
    ))


def _post_scrfd(outputs, meta, confidence, iou):
    reshaped = []
    widths = [1, 1, 1, 4, 4, 4, 10, 10, 10]
    for output, width in zip(outputs, widths):
        reshaped.append(np.asarray(output).reshape(-1, width))
    all_boxes, all_scores, all_landmarks = [], [], []
    for level, stride in enumerate((8, 16, 32)):
        scores = reshaped[level].reshape(-1)
        bbox = reshaped[level + 3] * stride
        landmarks = reshaped[level + 6] * stride
        height, width = 640 // stride, 640 // stride
        centers = np.stack(np.mgrid[:height, :width][::-1], axis=-1).astype(np.float32)
        centers = (centers * stride).reshape(-1, 2)
        centers = np.stack([centers, centers], axis=1).reshape(-1, 2)
        indices = np.where(scores >= confidence)[0]
        if not indices.size:
            continue
        boxes = _distance2bbox(centers, bbox)[indices]
        kps = np.empty((len(centers), 5, 2), dtype=np.float32)
        for point in range(5):
            kps[:, point, 0] = centers[:, 0] + landmarks[:, point * 2]
            kps[:, point, 1] = centers[:, 1] + landmarks[:, point * 2 + 1]
        all_boxes.append(boxes)
        all_scores.append(scores[indices])
        all_landmarks.append(kps[indices])
    if not all_boxes:
        return {"faces": [], "count": 0}
    boxes = _restore_xyxy(np.vstack(all_boxes), meta)
    scores = np.concatenate(all_scores)
    landmarks = np.vstack(all_landmarks)
    ratio = float(meta["ratio"])
    landmarks /= ratio
    keep = _nms(boxes, scores, iou)
    faces = []
    for index in keep:
        faces.append({
            "box": [round(float(v), 2) for v in boxes[index]],
            "score": round(float(scores[index]), 6),
            "landmarks": [[round(float(x), 2), round(float(y), 2)] for x, y in landmarks[index]],
        })
    return {"faces": faces, "count": len(faces)}


def _retina_priors(width=1024, height=624):
    priors = []
    for min_sizes, step in zip(((16, 32), (64, 128), (256, 512)), (8, 16, 32)):
        for row in range(math.ceil(height / step)):
            for col in range(math.ceil(width / step)):
                for size in min_sizes:
                    priors.append(((col + 0.5) * step / width, (row + 0.5) * step / height, size / width, size / height))
    return np.asarray(priors, dtype=np.float32)


RETINA_PRIORS = _retina_priors()


def _post_retinaface(outputs, meta, confidence, iou):
    loc = np.asarray(outputs[0]).reshape(-1, 4)
    conf = np.asarray(outputs[1]).reshape(-1, 2)[:, 1]
    landm = np.asarray(outputs[2]).reshape(-1, 10)
    priors = RETINA_PRIORS
    centers = priors[:, :2] + loc[:, :2] * 0.1 * priors[:, 2:]
    sizes = priors[:, 2:] * np.exp(loc[:, 2:] * 0.2)
    boxes = np.column_stack((centers - sizes / 2, centers + sizes / 2))
    boxes *= np.array([1024, 624, 1024, 624], dtype=np.float32)
    landmarks = np.empty((len(priors), 5, 2), dtype=np.float32)
    for point in range(5):
        landmarks[:, point] = priors[:, :2] + landm[:, point * 2:point * 2 + 2] * 0.1 * priors[:, 2:]
    landmarks *= np.array([1024, 624], dtype=np.float32)
    selected = conf >= confidence
    boxes, conf, landmarks = boxes[selected], conf[selected], landmarks[selected]
    if not len(boxes):
        return {"faces": [], "count": 0}
    boxes = _restore_xyxy(boxes, meta)
    scale_x, scale_y = meta["ratio"]
    landmarks[:, :, 0] /= scale_x
    landmarks[:, :, 1] /= scale_y
    keep = _nms(boxes, conf, iou)
    faces = [{
        "box": [round(float(v), 2) for v in boxes[i]],
        "score": round(float(conf[i]), 6),
        "landmarks": [[round(float(x), 2), round(float(y), 2)] for x, y in landmarks[i]],
    } for i in keep]
    return {"faces": faces, "count": len(faces)}


def _post_bytetrack(outputs, meta, confidence, iou):
    rows = np.asarray(outputs[0]).reshape(1, 13566, 6).copy()
    grids, expanded = [], []
    for stride in (8, 16, 32):
        h, w = 608 // stride, 1088 // stride
        yv, xv = np.meshgrid(np.arange(h), np.arange(w), indexing="ij")
        grids.append(np.stack((xv, yv), 2).reshape(1, -1, 2))
        expanded.append(np.full((1, h * w, 1), stride))
    grid = np.concatenate(grids, 1)
    stride = np.concatenate(expanded, 1)
    rows[..., :2] = (rows[..., :2] + grid) * stride
    rows[..., 2:4] = np.exp(np.clip(rows[..., 2:4], -20, 20)) * stride
    rows = rows[0]
    scores = rows[:, 4] * rows[:, 5]
    selected = scores >= confidence
    rows, scores = rows[selected], scores[selected]
    if not len(rows):
        return {"detections": [], "count": 0, "tracking_note": "detector API; IDs require sequential video frames"}
    boxes = np.column_stack((rows[:, 0] - rows[:, 2] / 2, rows[:, 1] - rows[:, 3] / 2,
                             rows[:, 0] + rows[:, 2] / 2, rows[:, 1] + rows[:, 3] / 2))
    boxes = _restore_xyxy(boxes, meta)
    keep = _nms(boxes, scores, iou)
    detections = _box_records(boxes, scores, np.zeros(len(scores), dtype=np.int32), keep)
    return {"detections": detections, "count": len(detections), "tracking_note": "detector API; IDs require sequential video frames"}


def _post_yolov5_face(outputs, meta, confidence, iou):
    anchors = np.asarray((((4, 5), (8, 10), (13, 16)), ((23, 29), (43, 55), (73, 105)),
                          ((146, 217), (231, 300), (335, 433))), dtype=np.float32)
    decoded = []
    for level, (output, stride) in enumerate(zip(outputs, (8, 16, 32))):
        h = 640 // stride
        values = np.asarray(output).reshape(1, 3, 16, h, h).transpose(0, 1, 3, 4, 2)[0]
        yv, xv = np.meshgrid(np.arange(h), np.arange(h), indexing="ij")
        grid = np.stack((xv, yv), axis=-1)[None, ...]
        xy = (_sigmoid(values[..., :2]) * 2.0 - 0.5 + grid) * stride
        wh = (_sigmoid(values[..., 2:4]) * 2.0) ** 2 * anchors[level][:, None, None, :]
        obj = _sigmoid(values[..., 4])
        cls = _sigmoid(values[..., 15])
        score = obj * cls
        lm = values[..., 5:15].copy().reshape(3, h, h, 5, 2)
        lm = lm * anchors[level][:, None, None, None, :] + grid[:, :, :, None, :] * stride
        part = np.concatenate((xy, wh, score[..., None], lm.reshape(3, h, h, 10)), axis=-1)
        decoded.append(part.reshape(-1, 15))
    rows = np.vstack(decoded)
    rows = rows[rows[:, 4] >= confidence]
    if not len(rows):
        return {"faces": [], "count": 0}
    boxes = np.column_stack((rows[:, 0] - rows[:, 2] / 2, rows[:, 1] - rows[:, 3] / 2,
                             rows[:, 0] + rows[:, 2] / 2, rows[:, 1] + rows[:, 3] / 2))
    boxes = _restore_xyxy(boxes, meta)
    scores = rows[:, 4]
    keep = _nms(boxes, scores, iou)
    scale_x, scale_y = meta["ratio"]
    faces = []
    for index in keep:
        points = rows[index, 5:].reshape(5, 2)
        points[:, 0] /= scale_x
        points[:, 1] /= scale_y
        faces.append({"box": [round(float(v), 2) for v in boxes[index]],
                      "score": round(float(scores[index]), 6),
                      "landmarks": [[round(float(x), 2), round(float(y), 2)] for x, y in points]})
    return {"faces": faces, "count": len(faces)}


def _summaries(outputs):
    result = []
    for output in outputs:
        array = np.asarray(output)
        result.append({
            "shape": list(array.shape), "elements": int(array.size),
            "min": round(float(array.min()), 6) if array.size else None,
            "max": round(float(array.max()), 6) if array.size else None,
            "mean": round(float(array.mean()), 6) if array.size else None,
        })
    return result


def _postprocess(model_id, outputs, spec, meta, confidence, iou):
    task = spec["task"]
    if task == "image-classification":
        scores = np.asarray(outputs[0]).reshape(-1)
        top = np.argsort(scores)[::-1][:5]
        return {"top5": [{"class_id": int(i), "score": round(float(scores[i]), 6)} for i in top]}
    if spec.get("postprocess") in {"ultralytics", "ultralytics-obb", "yolov8-seg"}:
        result = _post_ultralytics(outputs, spec, meta, confidence, iou)
        if spec.get("postprocess") == "yolov8-seg":
            result["mask_proto_shape"] = [1, 32, 160, 160]
            result["mask_note"] = "NPU mask prototype produced; API currently returns boxes and mask tensor metadata"
        return result
    if model_id == "scrfd":
        return _post_scrfd(outputs, meta, confidence, iou)
    if model_id == "retinaface":
        return _post_retinaface(outputs, meta, confidence, iou)
    if model_id == "yolov5s_face":
        return _post_yolov5_face(outputs, meta, confidence, iou)
    if model_id == "bytetrack_s":
        return _post_bytetrack(outputs, meta, confidence, iou)
    if model_id == "arcface":
        vector = np.asarray(outputs[0]).reshape(-1).astype(np.float64)
        norm = float(np.linalg.norm(vector))
        if norm:
            vector /= norm
        return {"dimension": int(vector.size), "l2_normalized": True,
                "embedding": [round(float(v), 7) for v in vector]}
    return {"outputs": _summaries(outputs)}


class EngineManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.model_id = None
        self.engine = None

    def close(self):
        with self.lock:
            if self.engine is not None:
                self.engine.clean()
                self.engine = None
                self.model_id = None

    def infer(self, model_id: str, data: bytes, confidence: float, iou: float) -> dict:
        spec = MODELS[model_id]
        tensor, meta = _prepare_image(data, spec)
        with self.lock:
            load_ms = 0.0
            if self.model_id != model_id:
                if self.engine is not None:
                    self.engine.clean()
                started = time.perf_counter()
                self.engine = EngineInfer(_model_path(model_id))
                self.model_id = model_id
                load_ms = (time.perf_counter() - started) * 1000.0
            started = time.perf_counter()
            model_input = [tensor] if spec.get("as_list") else tensor
            outputs = self.engine.forward(model_input)
            inference_ms = (time.perf_counter() - started) * 1000.0
            result = _postprocess(model_id, outputs, spec, meta, confidence, iou)
        return {
            "ok": True, "model": model_id, "task": spec["task"], "backend": "CIX P1 NPU (NOE)",
            "input_shape": list(tensor.shape), "original_size": meta["original_size"],
            "logical_output_shapes": spec["logical_outputs"], "runtime_outputs": _summaries(outputs),
            "load_ms": round(load_ms, 3), "inference_ms": round(inference_ms, 3), "result": result,
        }


MANAGER = EngineManager()


def _model_listing():
    entries = []
    for model_id, spec in MODELS.items():
        path = _model_path(model_id)
        entries.append({
            "id": model_id, "task": spec["task"], "path": path,
            "available": os.path.isfile(path), "bytes": os.path.getsize(path) if os.path.isfile(path) else None,
            "loaded": MANAGER.model_id == model_id,
        })
    return entries


class Handler(BaseHTTPRequestHandler):
    server_version = "cix-cv-noe/1.0"

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def _json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/health":
            available = sum(1 for item in _model_listing() if item["available"])
            self._json(HTTPStatus.OK, {"ok": True, "service": "cix-cv-noe", "device": os.path.exists("/dev/aipu"),
                                       "available_models": available, "loaded_model": MANAGER.model_id})
        elif path in {"/v1/cv/models", "/v1/models"}:
            self._json(HTTPStatus.OK, {"object": "list", "data": _model_listing()})
        else:
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        prefix = "/v1/cv/infer/"
        if not parsed.path.startswith(prefix):
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
            return
        model_id = parsed.path[len(prefix):].strip("/")
        if model_id not in MODELS:
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": f"unknown model: {model_id}"})
            return
        if not os.path.isfile(_model_path(model_id)):
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "model file is missing"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": "invalid body size"})
            return
        data = self.rfile.read(length)
        if self.headers.get("Content-Type", "").startswith("application/json"):
            try:
                data = base64.b64decode(json.loads(data)["image_base64"], validate=True)
            except Exception as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"invalid base64 JSON: {exc}"})
                return
        query = parse_qs(parsed.query)
        confidence = float(query.get("confidence", ["0.25"])[0])
        iou = float(query.get("iou", ["0.45"])[0])
        try:
            result = MANAGER.infer(model_id, data, confidence, iou)
            self._json(HTTPStatus.OK, result)
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "model": model_id, "error": str(exc)})
        except Exception as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "model": model_id,
                                                          "error": f"{type(exc).__name__}: {exc}"})


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)

    def stop(_signum, _frame):
        MANAGER.close()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(json.dumps({"event": "start", "host": HOST, "port": PORT, "models": len(MODELS)}), flush=True)
    try:
        server.serve_forever()
    finally:
        MANAGER.close()
        server.server_close()


if __name__ == "__main__":
    main()
