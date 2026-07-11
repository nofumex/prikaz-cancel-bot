from __future__ import annotations

from pathlib import Path


def normalize_order_upload(path: str | Path) -> Path:
    source = Path(path)
    suffix = source.suffix.lower()
    if suffix == '.pdf':
        import fitz

        document = fitz.open(source)
        if not document.page_count:
            raise ValueError('PDF не содержит страниц.')
        output = source.with_suffix('.jpg')
        pixmap = document[0].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        pixmap.save(output)
        document.close()
        return output
    if suffix in {'.heic', '.heif'}:
        from pillow_heif import register_heif_opener
        from PIL import Image

        register_heif_opener()
        output = source.with_suffix('.jpg')
        with Image.open(source) as image:
            image.convert('RGB').save(output, 'JPEG', quality=95)
        return output
    return source
