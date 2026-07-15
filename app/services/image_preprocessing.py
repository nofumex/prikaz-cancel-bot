from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageEnhance

from app.utils import ensure_dir


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
