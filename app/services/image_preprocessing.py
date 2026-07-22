from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from app.utils import ensure_dir

PREPROCESSING_VERSION = "order-gray-v1"


@dataclass(frozen=True, slots=True)
class ImageQuality:
    ok: bool
    reason: str = ""
    width: int = 0
    height: int = 0


def assess_order_image(order_photo_path: str | Path) -> ImageQuality:
    """Cheap, deliberately permissive input check before OCR/LLM work."""
    try:
        with Image.open(order_photo_path) as source:
            image = ImageOps.exif_transpose(source)
            width, height = image.size
            # Telegram commonly downsizes tall screenshots to ~500-600 px in
            # width. They still contain enough document pixels for OCR, so do
            # not reject them solely for missing the old 600 px short-side
            # threshold. Truly tiny inputs remain blocked by both limits.
            if min(width, height) < 400 or width * height < 500_000:
                return ImageQuality(False, "Изображение слишком маленькое; сфотографируйте весь приказ ближе.", width, height)
            sample = image.convert("L")
            sample.thumbnail((900, 900))
            histogram = sample.filter(ImageFilter.FIND_EDGES).histogram()
            pixels = max(1, sum(histogram))
            edge_mean = sum(level * count for level, count in enumerate(histogram)) / pixels
            if edge_mean < 2.0:
                return ImageQuality(False, "Текст сильно размыт; сделайте фото при хорошем освещении.", width, height)
            return ImageQuality(True, width=width, height=height)
    except (OSError, ValueError):
        return ImageQuality(False, "Файл изображения не открывается.")


def prepare_order_ocr_image(order_photo_path: str | Path, *, case_id: int | None = None) -> Path:
    """Create the sole conservative full-page image used by main Tesseract."""
    source = Path(order_photo_path)
    if not source.exists():
        return source
    debug_dir = ensure_dir(Path("storage/debug") / f"case_{case_id or 'unknown'}")
    target = debug_dir / f"order_{PREPROCESSING_VERSION}_{source.stem}.png"
    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened).convert("L")
        image = ImageOps.autocontrast(image, cutoff=0.5)
        image = ImageEnhance.Contrast(image).enhance(1.15)
        image = ImageEnhance.Sharpness(image).enhance(1.15)
        image.save(target, optimize=True)
    return target


def _enhance_for_ocr(image: Image.Image) -> Image.Image:
    gray = image.convert("L").convert("RGB")
    contrast = ImageEnhance.Contrast(gray).enhance(1.35)
    return ImageEnhance.Sharpness(contrast).enhance(1.2)


def build_order_verifier_image(order_photo_path: str | Path, *, case_id: int | None = None) -> Path:
    """Create a full-page alternate rendering for the independent verifier."""
    source = Path(order_photo_path)
    if not source.exists():
        return source
    debug_dir = ensure_dir(Path("storage/debug") / f"case_{case_id or 'unknown'}")
    target = debug_dir / f"order_verifier_{source.stem}.jpg"
    with Image.open(source) as img:
        enhanced = _enhance_for_ocr(img.convert("RGB"))
        enhanced.save(target, quality=95)
    return target


def build_order_ocr_variants(order_photo_path: str | Path, *, case_id: int | None = None) -> list[Path]:
    """Create a full image plus overlapping 2x vertical tiles.

    Screenshots and distant photos often devote most pixels to margins or app
    chrome. Tiling preserves the original context while giving the model
    enough pixels to compare individual letters and kopeks.
    """
    source = Path(order_photo_path)
    if not source.exists():
        return [source]
    debug_dir = ensure_dir(Path("storage/debug") / f"case_{case_id or 'unknown'}")
    variants: list[Path] = []
    with Image.open(source) as img:
        img = img.convert("RGB")
        full_path = debug_dir / f"order_ocr_full_{source.stem}.jpg"
        _enhance_for_ocr(img).save(full_path, quality=95)
        variants.append(full_path)
        width, height = img.size
        spans = ((0.0, 0.55, "top"), (0.20, 0.78, "middle"), (0.45, 1.0, "bottom"))
        for start, end, label in spans:
            tile = img.crop((0, int(height * start), width, int(height * end)))
            tile = tile.resize((tile.width * 2, tile.height * 2), Image.Resampling.LANCZOS)
            path = debug_dir / f"order_ocr_{label}_2x_{source.stem}.jpg"
            _enhance_for_ocr(tile).save(path, quality=95)
            variants.append(path)
    return variants


def build_amount_ocr_variants(order_photo_path: str | Path, *, case_id: int | None = None) -> list[Path]:
    """Build full image and cropped/zoomed variants for targeted amount OCR."""
    return build_order_ocr_variants(order_photo_path, case_id=case_id)
