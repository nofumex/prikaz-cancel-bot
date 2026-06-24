from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

try:
    import fitz  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    fitz = None


def _find_soffice() -> str | None:
    for name in ("soffice", "libreoffice"):
        path = shutil.which(name)
        if path:
            return path
    return None


def convert_docx_to_pdf(docx_path: str | Path, outdir: str | Path | None = None) -> Path:
    soffice = _find_soffice()
    if not soffice:
        raise RuntimeError("LibreOffice/soffice is not available for DOCX -> PDF conversion")
    docx_path = Path(docx_path)
    outdir_path = Path(outdir or docx_path.parent)
    outdir_path.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(outdir_path), str(docx_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to convert DOCX to PDF: {result.stderr.strip() or result.stdout.strip()}")
    pdf_path = outdir_path / (docx_path.stem + ".pdf")
    if not pdf_path.exists():
        raise RuntimeError(f"LibreOffice reported success but did not create {pdf_path}")
    return pdf_path


def create_preview_pdf(full_pdf_path: str | Path, preview_pdf_path: str | Path) -> Path:
    if fitz is None:
        raise RuntimeError("PyMuPDF (fitz) is not available for preview PDF generation")
    full_pdf_path = Path(full_pdf_path)
    preview_pdf_path = Path(preview_pdf_path)
    doc = fitz.open(full_pdf_path)
    try:
        for page in doc:
            page_dict = page.get_text("dict")
            lines = []
            for block in page_dict.get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    spans = [span for span in line.get("spans", []) if str(span.get("text") or "").strip()]
                    if not spans:
                        continue
                    x0 = min(span["bbox"][0] for span in spans)
                    y0 = min(span["bbox"][1] for span in spans)
                    x1 = max(span["bbox"][2] for span in spans)
                    y1 = max(span["bbox"][3] for span in spans)
                    lines.append((y0, x0, fitz.Rect(x0, y0, x1, y1)))
            lines.sort(key=lambda item: (round(item[0], 1), item[1]))
            for index, (_, _, rect) in enumerate(lines):
                if index % 2 == 1:
                    pad = 1.5
                    highlight = fitz.Rect(rect.x0 - pad, rect.y0 - 0.5, rect.x1 + pad, rect.y1 + 0.5)
                    page.add_redact_annot(highlight, fill=(0.82, 0.82, 0.82))
            page.apply_redactions()
        doc.save(preview_pdf_path)
    finally:
        doc.close()
    return preview_pdf_path


def pdf_text(path: str | Path) -> str:
    if fitz is None:
        raise RuntimeError("PyMuPDF (fitz) is not available for PDF text validation")
    document = fitz.open(str(path))
    try:
        return "\n".join(page.get_text("text") for page in document)
    finally:
        document.close()
