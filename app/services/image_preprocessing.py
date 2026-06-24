from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageEnhance

from app.utils import ensure_dir


def _enhance_for_ocr(image: Image.Image) -> Image.Image:
    gray = image.convert("L").convert("RGB")
    contrast = ImageEnhance.Contrast(gray).enhance(1.35)
    return ImageEnhance.Sharpness(contrast).enhance(1.2)


def build_amount_ocr_variants(order_photo_path: str | Path, *, case_id: int | None = None) -> list[Path]:
    """Build full image and cropped/zoomed variants for targeted amount OCR."""
    source = Path(order_photo_path)
    if not source.exists():
        return [source]

    debug_dir = ensure_dir(Path("storage/debug") / f"case_{case_id or 'unknown'}")
    variants: list[Path] = []

    with Image.open(source) as img:
        img = img.convert("RGB")
        full_path = debug_dir / "amount_ocr_full.jpg"
        _enhance_for_ocr(img).save(full_path, quality=95)
        variants.append(full_path)

        width, height = img.size
        bottom = img.crop((0, height // 2, width, height))
        bottom_path = debug_dir / "amount_ocr_bottom_half.jpg"
        _enhance_for_ocr(bottom).save(bottom_path, quality=95)
        variants.append(bottom_path)

        zoom_w, zoom_h = max(1, width // 2), max(1, height // 2)
        zoom_x = max(0, (width - zoom_w) // 2)
        zoom_y = max(0, height - zoom_h)
        zoom = img.crop((zoom_x, zoom_y, zoom_x + zoom_w, zoom_y + zoom_h))
        zoom_2x = zoom.resize((zoom_w * 2, zoom_h * 2), Image.Resampling.LANCZOS)
        zoom_path = debug_dir / "amount_ocr_bottom_zoom2x.jpg"
        _enhance_for_ocr(zoom_2x).save(zoom_path, quality=95)
        variants.append(zoom_path)

    return variants
