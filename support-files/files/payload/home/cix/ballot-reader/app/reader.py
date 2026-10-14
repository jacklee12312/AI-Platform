from __future__ import annotations

import math
import os
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pypdfium2 as pdfium


MODEL_ID = "democratic-eval-v1"
PERSON_NAMES = ("赵一", "钱二", "王三", "李四", "周五", "郑六")
DIMENSION_NAMES = tuple(f"维度{i}" for i in range(1, 7))
GRADE_NAMES = ("好", "较好", "一般", "差")


class BallotError(ValueError):
    def __init__(self, code: str, message: str, diagnostics: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.diagnostics = diagnostics or {}


@dataclass(frozen=True)
class Layout:
    rows: int = int(os.getenv("BALLOT_EXPECTED_ROWS", "6"))
    dimensions: int = int(os.getenv("BALLOT_EXPECTED_DIMENSIONS", "6"))
    grades: int = int(os.getenv("BALLOT_GRADES_PER_DIMENSION", "4"))

    @property
    def option_columns(self) -> int:
        return self.dimensions * self.grades

    @property
    def expected_marks(self) -> int:
        return self.rows * self.dimensions


def decode_document(data: bytes, filename: str = "document.bin") -> list[np.ndarray]:
    if not data:
        raise BallotError("empty_document", "文档为空")
    if data.startswith(b"%PDF") or filename.lower().endswith(".pdf"):
        return _decode_pdf(data)
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise BallotError("unsupported_document", "无法解码图片或 PDF")
    return [image]


def _decode_pdf(data: bytes) -> list[np.ndarray]:
    try:
        document = pdfium.PdfDocument(BytesIO(data))
    except Exception as exc:
        raise BallotError("invalid_pdf", f"PDF 打开失败: {exc}") from exc
    max_pages = int(os.getenv("BALLOT_MAX_PAGES", "20"))
    if len(document) > max_pages:
        raise BallotError("too_many_pages", f"PDF 页数 {len(document)} 超过限制 {max_pages}")
    long_edge = int(os.getenv("BALLOT_RENDER_LONG_EDGE", "3000"))
    pages: list[np.ndarray] = []
    for page_number in range(len(document)):
        page = document[page_number]
        width, height = page.get_size()
        scale = max(1.0, long_edge / max(width, height))
        bitmap = page.render(scale=scale)
        pixels = bitmap.to_numpy()
        if pixels.ndim == 2:
            image = cv2.cvtColor(pixels, cv2.COLOR_GRAY2BGR)
        elif pixels.shape[2] == 4:
            image = cv2.cvtColor(pixels, cv2.COLOR_BGRA2BGR)
        else:
            image = cv2.cvtColor(pixels, cv2.COLOR_RGB2BGR)
        pages.append(np.ascontiguousarray(image))
    return pages


def _adaptive_ink(gray: np.ndarray) -> np.ndarray:
    minimum = min(gray.shape[:2])
    block = max(31, min(81, (minimum // 24) | 1))
    return cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, block, 13
    )


def _rotate_bound(image: np.ndarray, angle: float) -> np.ndarray:
    height, width = image.shape[:2]
    center = (width / 2.0, height / 2.0)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    cosine, sine = abs(matrix[0, 0]), abs(matrix[0, 1])
    new_width = int(height * sine + width * cosine)
    new_height = int(height * cosine + width * sine)
    matrix[0, 2] += new_width / 2.0 - center[0]
    matrix[1, 2] += new_height / 2.0 - center[1]
    return cv2.warpAffine(
        image, matrix, (new_width, new_height), flags=cv2.INTER_CUBIC, borderValue=(255, 255, 255)
    )


def _normalize_orientation(image: np.ndarray) -> tuple[np.ndarray, int]:
    height, width = image.shape[:2]
    if height > width:
        return cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE), -90
    return image.copy(), 0


def _estimate_skew(gray: np.ndarray) -> float:
    height, width = gray.shape[:2]
    scale = min(1.0, 1800.0 / max(height, width))
    sample = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else gray
    edges = cv2.Canny(sample, 60, 160)
    lines = cv2.HoughLinesP(
        edges,
        1,
        np.pi / 720,
        threshold=max(80, sample.shape[1] // 10),
        minLineLength=sample.shape[1] // 5,
        maxLineGap=25,
    )
    if lines is None:
        return 0.0
    angles: list[float] = []
    for x1, y1, x2, y2 in lines[:, 0]:
        angle = math.degrees(math.atan2(float(y2 - y1), float(x2 - x1)))
        while angle <= -90:
            angle += 180
        while angle > 90:
            angle -= 180
        if abs(angle) <= 8:
            length = math.hypot(x2 - x1, y2 - y1)
            angles.extend([angle] * max(1, int(length / 250)))
    return float(np.median(angles)) if angles else 0.0


def _deskew(image: np.ndarray) -> tuple[np.ndarray, float]:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    angle = _estimate_skew(gray)
    if abs(angle) < 0.08:
        return image, 0.0
    return _rotate_bound(image, angle), angle


def _table_bbox(gray: np.ndarray) -> tuple[int, int, int, int]:
    height, width = gray.shape[:2]
    ink = _adaptive_ink(gray)
    horizontal = cv2.morphologyEx(
        ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(45, width // 35), 1))
    )
    vertical = cv2.morphologyEx(
        ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(35, height // 35)))
    )
    grid = cv2.dilate(cv2.bitwise_or(horizontal, vertical), np.ones((3, 3), np.uint8), iterations=1)
    count, _, stats, _ = cv2.connectedComponentsWithStats(grid, 8)
    candidates: list[tuple[float, tuple[int, int, int, int]]] = []
    for index in range(1, count):
        x, y, box_width, box_height, pixels = (int(value) for value in stats[index])
        aspect = box_width / max(1, box_height)
        if box_width < width * 0.42 or box_height < height * 0.16 or aspect < 1.8:
            continue
        coverage = pixels / max(1, box_width * box_height)
        score = box_width * box_height * (0.5 + min(coverage * 12.0, 1.5))
        candidates.append((score, (x, y, box_width, box_height)))
    if not candidates:
        raise BallotError(
            "table_not_found",
            "未检测到完整表格",
            {"image_width": width, "image_height": height},
        )
    _, (x, y, box_width, box_height) = max(candidates, key=lambda item: item[0])
    padding = max(2, min(width, height) // 700)
    x0, y0 = max(0, x - padding), max(0, y - padding)
    x1, y1 = min(width, x + box_width + padding), min(height, y + box_height + padding)
    return x0, y0, x1 - x0, y1 - y0


def _order_quad(points: np.ndarray) -> np.ndarray:
    points = points.reshape(4, 2).astype(np.float32)
    ordered = np.zeros((4, 2), dtype=np.float32)
    sums = points.sum(axis=1)
    differences = np.diff(points, axis=1).reshape(-1)
    ordered[0] = points[np.argmin(sums)]
    ordered[2] = points[np.argmax(sums)]
    ordered[1] = points[np.argmin(differences)]
    ordered[3] = points[np.argmax(differences)]
    return ordered


def _rectify_table(image: np.ndarray, bbox: tuple[int, int, int, int]) -> tuple[np.ndarray, list[list[float]]]:
    x, y, width, height = bbox
    crop = image[y : y + height, x : x + width]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    ink = _adaptive_ink(gray)
    horizontal = cv2.morphologyEx(
        ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(35, width // 35), 1))
    )
    vertical = cv2.morphologyEx(
        ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(25, height // 35)))
    )
    grid = cv2.dilate(cv2.bitwise_or(horizontal, vertical), np.ones((5, 5), np.uint8), iterations=1)
    contours, _ = cv2.findContours(grid, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return crop, [[0.0, 0.0], [float(width - 1), 0.0], [float(width - 1), float(height - 1)], [0.0, float(height - 1)]]
    contour = max(contours, key=cv2.contourArea)
    hull = cv2.convexHull(contour)
    perimeter = cv2.arcLength(hull, True)
    quad = cv2.approxPolyDP(hull, 0.018 * perimeter, True)
    if len(quad) != 4:
        quad = cv2.boxPoints(cv2.minAreaRect(hull)).reshape(4, 1, 2)
    ordered = _order_quad(quad)
    top_left, top_right, bottom_right, bottom_left = ordered
    target_width = int(round(max(np.linalg.norm(top_right - top_left), np.linalg.norm(bottom_right - bottom_left))))
    target_height = int(round(max(np.linalg.norm(bottom_left - top_left), np.linalg.norm(bottom_right - top_right))))
    if target_width < width * 0.80 or target_height < height * 0.80 or target_width / max(1, target_height) < 1.8:
        return crop, ordered.tolist()
    destination = np.array(
        [[0, 0], [target_width - 1, 0], [target_width - 1, target_height - 1], [0, target_height - 1]],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(ordered, destination)
    rectified = cv2.warpPerspective(
        crop,
        matrix,
        (target_width, target_height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(255, 255, 255),
    )
    return rectified, ordered.tolist()


def _cluster_projection(projection: np.ndarray, threshold: float, merge_gap: int = 7) -> list[int]:
    positions = np.flatnonzero(projection >= threshold)
    if positions.size == 0:
        return []
    groups = np.split(positions, np.where(np.diff(positions) > 1)[0] + 1)
    centers: list[int] = []
    for group in groups:
        weights = projection[group].astype(np.float64)
        center = int(round(float(np.average(group, weights=weights)))) if weights.sum() else int(group.mean())
        centers.append(center)
    merged: list[int] = []
    for center in centers:
        if merged and center - merged[-1] <= merge_gap:
            left, right = merged[-1], center
            merged[-1] = left if projection[left] >= projection[right] else right
        else:
            merged.append(center)
    return merged


def _grid_lines(table_gray: np.ndarray, layout: Layout) -> tuple[list[int], list[int], dict[str, Any]]:
    height, width = table_gray.shape[:2]
    ink = _adaptive_ink(table_gray)
    horizontal = cv2.morphologyEx(
        ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(35, width // 24), 1))
    )
    h_projection = np.count_nonzero(horizontal, axis=1)
    h_threshold = max(width * 0.35, float(h_projection.max()) * 0.43)
    all_y = _cluster_projection(h_projection, h_threshold)
    needed_y = layout.rows + 1
    if len(all_y) < needed_y:
        raise BallotError(
            "row_grid_incomplete",
            f"表格横线不足: 检测到 {len(all_y)}，至少需要 {needed_y}",
            {"horizontal_lines": all_y},
        )
    data_y = all_y[-needed_y:]
    body_top, body_bottom = data_y[0], data_y[-1]
    body = ink[body_top : body_bottom + 1, :]
    body_height = max(1, body.shape[0])
    vertical = cv2.morphologyEx(
        body,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(13, body_height // 3))),
    )
    v_projection = np.count_nonzero(vertical, axis=0)
    v_threshold = max(body_height * 0.42, float(v_projection.max()) * 0.48)
    all_x = _cluster_projection(v_projection, v_threshold)
    needed_x = layout.option_columns + 2
    if len(all_x) != needed_x:
        relaxed = _cluster_projection(v_projection, max(body_height * 0.25, float(v_projection.max()) * 0.30))
        if len(relaxed) == needed_x:
            all_x = relaxed
    if len(all_x) != needed_x:
        raise BallotError(
            "column_grid_incomplete",
            f"表格竖线数量异常: 检测到 {len(all_x)}，应为 {needed_x}",
            {"vertical_lines": all_x, "horizontal_lines": all_y},
        )
    diagnostics = {
        "horizontal_line_count": len(all_y),
        "vertical_line_count": len(all_x),
        "data_horizontal_lines": data_y,
        "data_vertical_lines": all_x,
    }
    return all_x, data_y, diagnostics


def _cell_score(cell_gray: np.ndarray) -> tuple[float, dict[str, float]]:
    height, width = cell_gray.shape[:2]
    mx = max(2, int(width * 0.13))
    my = max(2, int(height * 0.13))
    roi = cell_gray[my : max(my + 1, height - my), mx : max(mx + 1, width - mx)]
    if roi.size < 20:
        return 0.0, {"residual": 0.0, "ink_ratio": 0.0, "dark_ratio": 0.0}
    sigma = max(2.0, min(roi.shape[:2]) / 4.0)
    background = cv2.GaussianBlur(roi, (0, 0), sigmaX=sigma, sigmaY=sigma)
    residual = np.maximum(background.astype(np.int16) - roi.astype(np.int16), 0).astype(np.uint8)
    ink_ratio = float(np.mean(residual >= 14))
    dark_ratio = float(np.mean(roi <= 185))
    residual_mean = float(np.mean(np.maximum(residual.astype(np.float32) - 5.0, 0.0)))
    score = residual_mean + 34.0 * ink_ratio + 18.0 * dark_ratio
    return score, {
        "residual": round(residual_mean, 4),
        "ink_ratio": round(ink_ratio, 5),
        "dark_ratio": round(dark_ratio, 5),
    }


def _two_cluster_threshold(values: np.ndarray) -> tuple[float, float, float]:
    low, high = np.percentile(values, [35, 85]).astype(float)
    if high <= low:
        high = low + 1e-6
    for _ in range(30):
        distances_low = np.abs(values - low)
        distances_high = np.abs(values - high)
        high_mask = distances_high < distances_low
        if high_mask.all() or not high_mask.any():
            break
        new_low = float(values[~high_mask].mean())
        new_high = float(values[high_mask].mean())
        if abs(new_low - low) + abs(new_high - high) < 1e-6:
            low, high = new_low, new_high
            break
        low, high = new_low, new_high
    if low > high:
        low, high = high, low
    return (low + high) / 2.0, low, high


def analyze_page(image: np.ndarray, page_number: int = 1, source_name: str = "document") -> tuple[dict[str, Any], np.ndarray]:
    layout = Layout()
    oriented, orientation = _normalize_orientation(image)
    deskewed, skew = _deskew(oriented)
    gray = cv2.cvtColor(deskewed, cv2.COLOR_BGR2GRAY)
    x, y, width, height = _table_bbox(gray)
    table_image, table_quad = _rectify_table(deskewed, (x, y, width, height))
    table_gray = cv2.cvtColor(table_image, cv2.COLOR_BGR2GRAY)
    vertical_lines, horizontal_lines, diagnostics = _grid_lines(table_gray, layout)

    raw_cells: list[dict[str, Any]] = []
    scores = np.zeros((layout.rows, layout.option_columns), dtype=np.float64)
    for row in range(layout.rows):
        y0, y1 = horizontal_lines[row], horizontal_lines[row + 1]
        for column in range(layout.option_columns):
            x0, x1 = vertical_lines[column + 1], vertical_lines[column + 2]
            score, features = _cell_score(table_gray[y0:y1, x0:x1])
            scores[row, column] = score
            raw_cells.append(
                {
                    "row": row,
                    "column": column,
                    "score": round(score, 5),
                    "features": features,
                }
            )

    threshold, blank_center, mark_center = _two_cluster_threshold(scores.ravel())
    cluster_gap = max(0.05, mark_center - blank_center)
    selections: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    overlay_top = 60
    overlay = cv2.copyMakeBorder(table_image, overlay_top, 0, 0, 0, cv2.BORDER_CONSTANT, value=(255, 255, 255))
    detected_mark_count = int(np.count_nonzero(scores >= threshold))

    for row in range(layout.rows):
        for dimension in range(layout.dimensions):
            start = dimension * layout.grades
            group = scores[row, start : start + layout.grades]
            order = np.argsort(group)[::-1]
            best_grade, second_grade = int(order[0]), int(order[1])
            best, second = float(group[best_grade]), float(group[second_grade])
            above = [int(index) for index in np.flatnonzero(group >= threshold)]
            margin = best - second
            normalized_margin = margin / cluster_gap
            reasons: list[str] = []
            if len(above) == 0:
                reasons.append("no_mark_above_threshold")
            elif len(above) > 1:
                reasons.append("multiple_marks_above_threshold")
            if normalized_margin < 0.24:
                reasons.append("low_separation")
            confidence = float(np.clip(0.55 + normalized_margin * 0.32, 0.0, 0.995))
            column = start + best_grade
            selected = {
                "person_index": row,
                "person": PERSON_NAMES[row] if row < len(PERSON_NAMES) else f"人员{row + 1}",
                "dimension_index": dimension,
                "dimension": DIMENSION_NAMES[dimension] if dimension < len(DIMENSION_NAMES) else f"维度{dimension + 1}",
                "grade_index": best_grade,
                "grade": GRADE_NAMES[best_grade] if best_grade < len(GRADE_NAMES) else f"等级{best_grade + 1}",
                "score": round(best, 5),
                "runner_up_score": round(second, 5),
                "confidence": round(confidence, 4),
                "review_required": bool(reasons),
                "review_reasons": reasons,
            }
            selections.append(selected)
            if reasons:
                reviews.append(selected)
            x0, x1 = vertical_lines[column + 1], vertical_lines[column + 2]
            y0, y1 = horizontal_lines[row], horizontal_lines[row + 1]
            color = (0, 170, 0) if not reasons else (0, 140, 255)
            cv2.rectangle(overlay, (x0 + 2, overlay_top + y0 + 2), (x1 - 2, overlay_top + y1 - 2), color, 3)

    status = "accepted" if not reviews and detected_mark_count == layout.expected_marks else "review"
    if detected_mark_count != layout.expected_marks:
        reviews.insert(
            0,
            {
                "scope": "page",
                "review_required": True,
                "review_reasons": ["unexpected_mark_count"],
                "detected_mark_count": detected_mark_count,
                "expected_mark_count": layout.expected_marks,
            },
        )
    cv2.putText(
        overlay,
        f"{status} marks={detected_mark_count}/{layout.expected_marks} review={len(reviews)}",
        (15, 38),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.9,
        (0, 150, 0) if status == "accepted" else (0, 100, 255),
        2,
        cv2.LINE_AA,
    )
    result = {
        "page": page_number,
        "source": source_name,
        "status": status,
        "expected_mark_count": layout.expected_marks,
        "detected_mark_count": detected_mark_count,
        "selection_count": len(selections),
        "review_count": len(reviews),
        "selections": selections,
        "reviews": reviews,
        "diagnostics": {
            "orientation_degrees": orientation,
            "deskew_degrees": round(skew, 4),
            "table_bbox": [x, y, width, height],
            "table_quad_in_bbox": [[round(value, 2) for value in point] for point in table_quad],
            "rectified_size": [int(table_gray.shape[1]), int(table_gray.shape[0])],
            "score_threshold": round(threshold, 5),
            "blank_score_center": round(blank_center, 5),
            "mark_score_center": round(mark_center, 5),
            **diagnostics,
        },
        "cells": raw_cells,
    }
    return result, overlay


def encode_jpeg(image: np.ndarray, max_long_edge: int = 2400) -> bytes:
    height, width = image.shape[:2]
    if max(height, width) > max_long_edge:
        scale = max_long_edge / max(height, width)
        image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not ok:
        raise BallotError("overlay_encode_failed", "标注图编码失败")
    return encoded.tobytes()


def analyze_bytes(data: bytes, filename: str) -> tuple[dict[str, Any], list[bytes]]:
    pages = decode_document(data, filename)
    results: list[dict[str, Any]] = []
    overlays: list[bytes] = []
    for page_number, page in enumerate(pages, start=1):
        result, overlay = analyze_page(page, page_number=page_number, source_name=filename)
        results.append(result)
        overlays.append(encode_jpeg(overlay))
    return {
        "object": "document_analysis",
        "model": MODEL_ID,
        "filename": filename,
        "page_count": len(results),
        "status": "accepted" if all(item["status"] == "accepted" for item in results) else "review",
        "review_count": sum(item["review_count"] for item in results),
        "pages": results,
    }, overlays


def analyze_path(path: Path) -> tuple[dict[str, Any], list[bytes]]:
    return analyze_bytes(path.read_bytes(), path.name)
