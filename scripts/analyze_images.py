#!/usr/bin/env python3
"""Extract deterministic visual evidence from one image or an image collection."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from kunpeng_common import (
    IMAGE_EXTENSIONS,
    aggregate_status,
    atomic_write_json,
    bounded_error,
    configure_utf8,
    find_sources,
    prepare_output,
    quantile,
    relative_artifact,
    reused_analysis_status,
    sampled_fingerprint,
    source_id,
    status_counts,
    utc_now,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze images locally with OpenCV, Pillow, and optional PaddleOCR."
    )
    parser.add_argument("source", type=Path, help="Image file or directory.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--no-recursive", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--ocr", choices=("auto", "on", "off"), default="auto")
    parser.add_argument("--ocr-lang", default="ch")
    parser.add_argument("--ocr-device", default="auto")
    parser.add_argument("--max-images", type=int, default=1000)
    parser.add_argument("--contact-sheet-limit", type=int, default=64)
    return parser.parse_args()


def load_image(path: Path) -> tuple[Any, dict[str, Any]]:
    import numpy as np
    from PIL import Image, ImageOps

    with Image.open(path) as opened:
        frame_count = int(getattr(opened, "n_frames", 1))
        image = ImageOps.exif_transpose(opened.copy())
        has_alpha = "A" in image.getbands()
        rgb = image.convert("RGB")
        array = np.asarray(rgb)
        metadata = {
            "width": rgb.width,
            "height": rgb.height,
            "mode": opened.mode,
            "format": opened.format or path.suffix.lstrip(".").upper(),
            "has_alpha": has_alpha,
            "frame_count": frame_count,
        }
    return array, metadata


def dominant_palette(rgb: Any, colors: int = 6) -> list[dict[str, Any]]:
    import cv2
    import numpy as np

    height, width = rgb.shape[:2]
    scale = min(1.0, 320.0 / max(height, width))
    if scale < 1.0:
        rgb = cv2.resize(rgb, (max(1, round(width * scale)), max(1, round(height * scale))))
    pixels = rgb.reshape(-1, 3)
    if len(pixels) > 60000:
        indexes = np.linspace(0, len(pixels) - 1, 60000, dtype=np.int64)
        pixels = pixels[indexes]
    pixels = np.float32(pixels)
    cluster_count = max(1, min(colors, len(pixels)))
    cv2.setRNGSeed(42)
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.5)
    _, labels, centers = cv2.kmeans(
        pixels, cluster_count, None, criteria, 3, cv2.KMEANS_PP_CENTERS
    )
    counts = np.bincount(labels.flatten(), minlength=cluster_count)
    order = np.argsort(counts)[::-1]
    palette: list[dict[str, Any]] = []
    for index in order:
        red, green, blue = (int(round(value)) for value in centers[index])
        palette.append(
            {
                "hex": f"#{red:02X}{green:02X}{blue:02X}",
                "rgb": [red, green, blue],
                "share": round(float(counts[index] / counts.sum()), 4),
            }
        )
    return palette


def composition_metrics(rgb: Any) -> dict[str, Any]:
    import cv2
    import numpy as np

    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    median = float(np.median(gray))
    lower = int(max(0, 0.66 * median))
    upper = int(min(255, max(lower + 1, 1.33 * median)))
    edges = cv2.Canny(gray, lower, upper)
    blurred = cv2.GaussianBlur(gray, (0, 0), 7)
    attention = cv2.absdiff(gray, blurred).astype(np.float64) + edges.astype(np.float64)
    total_attention = float(attention.sum())
    height, width = gray.shape
    if total_attention:
        y_grid, x_grid = np.indices(gray.shape)
        center_x = float((attention * x_grid).sum() / total_attention / max(1, width - 1))
        center_y = float((attention * y_grid).sum() / total_attention / max(1, height - 1))
    else:
        center_x = center_y = 0.5

    horizontal_similarity = 1.0 - float(
        np.mean(np.abs(gray.astype(np.float32) - np.fliplr(gray).astype(np.float32))) / 255.0
    )
    vertical_similarity = 1.0 - float(
        np.mean(np.abs(gray.astype(np.float32) - np.flipud(gray).astype(np.float32))) / 255.0
    )
    white_mask = (gray >= 242) & (hsv[:, :, 1] <= 25)
    warm_balance = float(
        np.mean(rgb[:, :, 0].astype(np.float32) - rgb[:, :, 2].astype(np.float32)) / 255.0
    )
    luminance_values = gray.reshape(-1).astype(float)
    return {
        "brightness_mean": round(float(np.mean(gray)) / 255.0, 4),
        "luminance_p10": round(quantile(luminance_values, 0.10) / 255.0, 4),
        "luminance_p90": round(quantile(luminance_values, 0.90) / 255.0, 4),
        "contrast_std": round(float(np.std(gray)) / 255.0, 4),
        "saturation_mean": round(float(np.mean(hsv[:, :, 1])) / 255.0, 4),
        "warm_cool_balance": round(warm_balance, 4),
        "edge_density": round(float(np.count_nonzero(edges)) / edges.size, 4),
        "sharpness_laplacian": round(float(cv2.Laplacian(gray, cv2.CV_64F).var()), 2),
        "attention_centroid": {"x": round(center_x, 4), "y": round(center_y, 4)},
        "horizontal_symmetry": round(max(0.0, horizontal_similarity), 4),
        "vertical_symmetry": round(max(0.0, vertical_similarity), 4),
        "light_neutral_area": round(float(np.mean(white_mask)), 4),
    }


def polygon_area(points: list[list[float]]) -> float:
    if len(points) < 3:
        return 0.0
    return abs(
        sum(
            points[index][0] * points[(index + 1) % len(points)][1]
            - points[(index + 1) % len(points)][0] * points[index][1]
            for index in range(len(points))
        )
    ) / 2.0


def make_contact_sheet(entries: list[tuple[Path, str]], output: Path) -> None:
    from PIL import Image, ImageDraw, ImageOps

    if not entries:
        return
    cell_width, cell_height, label_height = 240, 180, 24
    columns = min(4, max(1, math.ceil(math.sqrt(len(entries)))))
    rows = math.ceil(len(entries) / columns)
    sheet = Image.new("RGB", (columns * cell_width, rows * (cell_height + label_height)), "white")
    draw = ImageDraw.Draw(sheet)
    for index, (path, label) in enumerate(entries):
        row, column = divmod(index, columns)
        x, y = column * cell_width, row * (cell_height + label_height)
        with Image.open(path) as opened:
            image = ImageOps.exif_transpose(opened.copy()).convert("RGB")
            fitted = ImageOps.contain(image, (cell_width, cell_height))
        offset = (x + (cell_width - fitted.width) // 2, y + (cell_height - fitted.height) // 2)
        sheet.paste(fitted, offset)
        draw.text((x + 6, y + cell_height + 4), label[:34], fill="black")
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output, quality=88, optimize=True)


def source_label(path: Path, root: Path) -> str:
    if root.is_dir():
        return path.relative_to(root.resolve()).as_posix()
    return path.name


def main() -> int:
    configure_utf8()
    args = parse_args()
    sources = find_sources(args.source, IMAGE_EXTENSIONS, not args.no_recursive)
    if not sources:
        raise SystemExit("No supported images found.")
    if len(sources) > max(1, args.max_images):
        raise SystemExit(f"Found {len(sources)} images; raise --max-images to process them all.")
    output = prepare_output(args.output, args.resume)

    ocr_engine = None
    ocr_error = None
    if args.ocr != "off":
        try:
            from local_ocr import LocalOCR

            ocr_engine = LocalOCR(args.ocr_lang, args.ocr_device)
        except Exception as exc:  # optional dependency/model initialization
            ocr_error = bounded_error(exc)

    root = args.source.resolve()
    manifest_items: list[dict[str, Any]] = []
    contact_entries: list[tuple[Path, str]] = []
    for index, path in enumerate(sources, start=1):
        item_id = source_id(path)
        item_dir = output / "images" / item_id
        analysis_path = item_dir / "analysis.json"
        label = source_label(path, root)
        if args.resume and analysis_path.exists():
            manifest_items.append(
                {
                    "id": item_id,
                    "source": label,
                    "status": reused_analysis_status(analysis_path),
                    "reused": True,
                    "analysis": relative_artifact(analysis_path, output),
                }
            )
            contact_entries.append((path, f"{index:03d} {path.name}"))
            continue

        try:
            rgb, metadata = load_image(path)
            height, width = rgb.shape[:2]
            ocr_lines: list[dict[str, Any]] = []
            stages: dict[str, dict[str, Any]] = {
                "metrics": {"status": "complete"},
            }
            if ocr_engine:
                try:
                    ocr_lines = ocr_engine.recognize(path)
                    stages["ocr"] = {"status": "complete"}
                except Exception as exc:
                    item_ocr_error = bounded_error(exc, path, output)
                    ocr_error = ocr_error or item_ocr_error
                    stages["ocr"] = {
                        "status": "partial",
                        "error": item_ocr_error,
                        "fallback": "host_visual_review",
                        "fallback_ready": True,
                        "host_review_required": True,
                    }
            elif args.ocr != "off":
                stages["ocr"] = {
                    "status": "partial",
                    "error": ocr_error,
                    "fallback": "host_visual_review",
                    "fallback_ready": True,
                    "host_review_required": True,
                }
            else:
                stages["ocr"] = {
                    "status": "not_applicable",
                    "reason": "disabled_by_user",
                }

            status = aggregate_status(stage["status"] for stage in stages.values())
            host_review_required = [
                name
                for name, stage in stages.items()
                if stage.get("host_review_required")
            ]

            text_area = sum(polygon_area(line.get("box", [])) for line in ocr_lines)
            analysis = {
                "schema_version": 2,
                "id": item_id,
                "status": status,
                "source": {"name": label, "fingerprint": sampled_fingerprint(path)},
                "image": {
                    **metadata,
                    "aspect_ratio": round(width / max(1, height), 5),
                    "orientation": "landscape" if width > height else "portrait" if height > width else "square",
                },
                "palette": dominant_palette(rgb),
                "visual_metrics": composition_metrics(rgb),
                "ocr": {
                    "line_count": len(ocr_lines),
                    "text_coverage_estimate": round(min(1.0, text_area / max(1, width * height)), 4),
                    "lines": ocr_lines,
                },
                "stages": stages,
                "host_review_required": host_review_required,
                "limitations": [
                    "Deterministic metrics do not identify subjects, intent, or hidden interactions.",
                    *(
                        [f"OCR unavailable or failed: {stages['ocr'].get('error')}"]
                        if stages["ocr"]["status"] == "partial"
                        else []
                    ),
                ],
            }
            atomic_write_json(analysis_path, analysis)
            manifest_items.append(
                {
                    "id": item_id,
                    "source": label,
                    "status": status,
                    "analysis": relative_artifact(analysis_path, output),
                    "host_review_required": host_review_required,
                }
            )
            contact_entries.append((path, f"{index:03d} {path.name}"))
        except Exception as exc:
            manifest_items.append(
                {
                    "id": item_id,
                    "source": label,
                    "status": "failed",
                    "error": bounded_error(exc, path, output),
                }
            )

    contact_path = output / "contact-sheet.jpg"
    try:
        make_contact_sheet(contact_entries[: max(0, args.contact_sheet_limit)], contact_path)
        contact_artifact = relative_artifact(contact_path, output) if contact_path.exists() else None
    except Exception as exc:
        contact_artifact = None
        ocr_error = ocr_error or bounded_error(exc, output=output)

    counts = status_counts(manifest_items)
    manifest = {
        "schema_version": 2,
        "kind": "image-analysis",
        "generated_at": utc_now(),
        "local_only": True,
        "source_count": len(sources),
        **counts,
        "contact_sheet": contact_artifact,
        "ocr": {"mode": args.ocr, "available": ocr_engine is not None, "error": ocr_error},
        "items": manifest_items,
    }
    atomic_write_json(output / "manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 1 if counts["failed_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
