from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import time
from collections import Counter
from pathlib import Path

from .reader import BallotError, analyze_path


SUPPORTED = {".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def run(input_dir: Path, output_dir: Path) -> int:
    files = sorted(
        (path for path in input_dir.iterdir() if path.is_file() and path.suffix.lower() in SUPPORTED),
        key=lambda path: (len(path.stem), path.name),
    )
    if not files:
        print(f"no supported documents in {input_dir}", file=sys.stderr)
        return 2
    output_dir.mkdir(parents=True, exist_ok=True)
    annotated_dir = output_dir / "annotated"
    annotated_dir.mkdir(exist_ok=True)
    documents: list[dict] = []
    detail_rows: list[dict] = []
    errors: list[dict] = []
    started = time.perf_counter()
    for source in files:
        file_started = time.perf_counter()
        sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
        try:
            result, overlays = analyze_path(source)
            result["sha256"] = sha256
            result["elapsed_seconds"] = round(time.perf_counter() - file_started, 4)
            documents.append(result)
            for page_index, page in enumerate(result["pages"], start=1):
                overlay_name = f"{source.stem}-p{page_index}.jpg"
                (annotated_dir / overlay_name).write_bytes(overlays[page_index - 1])
                for selection in page["selections"]:
                    detail_rows.append(
                        {
                            "ballot": source.name,
                            "page": page_index,
                            "person": selection["person"],
                            "dimension": selection["dimension"],
                            "grade": selection["grade"],
                            "confidence": selection["confidence"],
                            "review_required": selection["review_required"],
                            "review_reasons": "|".join(selection["review_reasons"]),
                        }
                    )
            print(f"{source.name}: {result['status']} review={result['review_count']} elapsed={result['elapsed_seconds']}s")
        except (BallotError, OSError) as exc:
            item = {
                "filename": source.name,
                "sha256": sha256,
                "code": getattr(exc, "code", "io_error"),
                "message": str(exc),
                "diagnostics": getattr(exc, "diagnostics", {}),
            }
            errors.append(item)
            print(f"{source.name}: error {item['code']} {item['message']}", file=sys.stderr)

    counts = Counter((row["person"], row["dimension"], row["grade"]) for row in detail_rows)
    summary_rows = [
        {"person": person, "dimension": dimension, "grade": grade, "count": count}
        for (person, dimension, grade), count in sorted(counts.items())
    ]
    review_rows = [row for row in detail_rows if row["review_required"]]
    manifest = {
        "model": "democratic-eval-v1",
        "input_dir": str(input_dir),
        "file_count": len(files),
        "success_count": len(documents),
        "error_count": len(errors),
        "accepted_count": sum(item["status"] == "accepted" for item in documents),
        "review_document_count": sum(item["status"] == "review" for item in documents),
        "review_selection_count": len(review_rows),
        "elapsed_seconds": round(time.perf_counter() - started, 4),
        "documents": documents,
        "errors": errors,
    }
    (output_dir / "results.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "review.json").write_text(json.dumps(review_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv(
        output_dir / "details.csv",
        ["ballot", "page", "person", "dimension", "grade", "confidence", "review_required", "review_reasons"],
        detail_rows,
    )
    _write_csv(output_dir / "summary.csv", ["person", "dimension", "grade", "count"], summary_rows)
    print(json.dumps({key: manifest[key] for key in manifest if key not in {"documents", "errors"}}, ensure_ascii=False))
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="批量识别民主测评票")
    parser.add_argument("input_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    return run(args.input_dir, args.output_dir)


if __name__ == "__main__":
    raise SystemExit(main())

