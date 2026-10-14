from __future__ import annotations

import cv2
import numpy as np

from app.reader import Layout, analyze_page


def synthetic_ballot() -> tuple[np.ndarray, list[list[int]]]:
    layout = Layout()
    table_x, table_y = 260, 300
    name_width, cell_width = 60, 42
    header_height, row_height = 280, 46
    table_width = name_width + layout.option_columns * cell_width
    table_height = header_height + layout.rows * row_height
    image = np.full((1200, 1700, 3), 248, dtype=np.uint8)
    cv2.rectangle(image, (table_x, table_y), (table_x + table_width, table_y + table_height), (70, 70, 70), 2)
    data_top = table_y + header_height
    for row in range(layout.rows + 1):
        y = data_top + row * row_height
        cv2.line(image, (table_x, y), (table_x + table_width, y), (80, 80, 80), 2)
    cv2.line(image, (table_x + name_width, table_y), (table_x + name_width, table_y + table_height), (80, 80, 80), 2)
    for column in range(layout.option_columns + 1):
        x = table_x + name_width + column * cell_width
        cv2.line(image, (x, table_y), (x, table_y + table_height), (80, 80, 80), 2)
    expected: list[list[int]] = []
    for row in range(layout.rows):
        selected: list[int] = []
        for dimension in range(layout.dimensions):
            grade = (row + dimension) % layout.grades
            selected.append(grade)
            column = dimension * layout.grades + grade
            x0 = table_x + name_width + column * cell_width
            y0 = data_top + row * row_height
            points = np.array(
                [[x0 + 10, y0 + 24], [x0 + 18, y0 + 33], [x0 + 34, y0 + 10]], dtype=np.int32
            )
            cv2.polylines(image, [points], False, (35, 35, 35), 3, cv2.LINE_AA)
        expected.append(selected)
    return image, expected


def test_synthetic_ballot() -> None:
    image, expected = synthetic_ballot()
    portrait = cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
    result, _ = analyze_page(portrait)
    assert result["selection_count"] == 36
    actual = [[None] * 6 for _ in range(6)]
    for item in result["selections"]:
        actual[item["person_index"]][item["dimension_index"]] = item["grade_index"]
    assert actual == expected

