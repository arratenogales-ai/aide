"""What file reads for the model share: which photos it accepts, which can be converted and
how they are converted.

A photo the model does not accept (HEIC) or that exceeds 5 MB is first converted to JPEG with sips,
which comes with macOS.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

MIN_PDF_TEXT = 80  # fewer characters than this: the PDF is taken as scanned
IMAGES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}  # the ones the model accepts
CONVERTIBLE = {".heic", ".heif", ".tif", ".tiff", ".bmp"}  # the ones sips converts to JPEG
SIPS = "/usr/bin/sips"
MAX_SIDE = 2000  # pixels of the longer side of a converted photo
MAX_IMAGE = 5 * 1024 * 1024  # the API limit per image (call.MAX_IMAGE)


class Unreadable(Exception):
    """There is nothing the model can read; the message is for the user."""


def _longer_side(path: Path) -> int:
    """The longer side of an image in pixels, according to sips (0 if it does not say)."""
    r = subprocess.run(
        [SIPS, "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return max((int(n) for n in re.findall(r"pixel(?:Width|Height):\s*(\d+)", r.stdout)), default=0)


def photo_for_model(path: Path, folder: Path, name: str) -> Path | None:
    """The photo as is if the model accepts it and it does not exceed 5 MB; otherwise, a JPEG
    copy of at most 2000 pixels (without enlarging it), made with sips. None if not possible."""
    suffix = path.suffix.lower()
    if suffix in IMAGES and path.stat().st_size <= MAX_IMAGE:
        return path
    if suffix not in IMAGES | CONVERTIBLE or not Path(SIPS).exists():
        return None
    destination = folder / f"{name}.jpg"
    try:
        size = ["-Z", str(MAX_SIDE)] if _longer_side(path) > MAX_SIDE else []
        subprocess.run(
            [SIPS, "-s", "format", "jpeg", "-s", "formatOptions", "85", *size, str(path)]
            + ["--out", str(destination)],
            check=True,
            capture_output=True,
            timeout=60,
        )
    except (subprocess.SubprocessError, OSError, ValueError) as e:
        log.warning("could not convert the photo %s: %s", path.name, e)
        return None
    if not destination.is_file() or destination.stat().st_size > MAX_IMAGE:
        return None
    return destination
