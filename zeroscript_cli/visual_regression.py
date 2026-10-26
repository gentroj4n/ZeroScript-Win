# SPDX-License-Identifier: GPL-3.0-or-later
"""Screenshot comparison helpers for browser/UI regression testing."""
from __future__ import annotations
import hashlib
from pathlib import Path
from typing import Any

def compare_images(baseline: str | Path, actual: str | Path, threshold: float = 0.01) -> dict[str, Any]:
    """Compare same-sized raster screenshots with Pillow; fail clearly when unavailable."""
    try:
        from PIL import Image, ImageChops, ImageStat
    except ImportError as exc:
        raise RuntimeError("Visual comparison requires Pillow. Install it in the selected Python environment.") from exc
    before_path, after_path = Path(baseline), Path(actual)
    with Image.open(before_path) as im:
        before = im.convert("RGBA")
    with Image.open(after_path) as im:
        after = im.convert("RGBA")
    if before.size != after.size:
        return {"passed": False, "reason": "dimensions_changed", "baseline_size": before.size, "actual_size": after.size, "changed_fraction": 1.0}
    diff = ImageChops.difference(before, after)
    pixels = list(diff.getdata())
    changed = sum(1 for px in pixels if any(channel > 0 for channel in px))
    fraction = changed / max(1, len(pixels))
    mean = sum(ImageStat.Stat(diff).mean) / 4
    return {
        "passed": fraction <= max(0.0, min(float(threshold), 1.0)),
        "reason": "within_threshold" if fraction <= max(0.0, min(float(threshold), 1.0)) else "visual_regression",
        "baseline": str(before_path), "actual": str(after_path), "size": before.size,
        "changed_pixels": changed, "total_pixels": len(pixels), "changed_fraction": fraction,
        "mean_absolute_channel_difference": mean,
        "baseline_sha256": hashlib.sha256(before_path.read_bytes()).hexdigest(),
        "actual_sha256": hashlib.sha256(after_path.read_bytes()).hexdigest(),
    }
