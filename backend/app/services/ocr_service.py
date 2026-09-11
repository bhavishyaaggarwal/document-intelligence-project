import io
import logging
from dataclasses import dataclass
from typing import Any
from pathlib import Path
import fitz
import pytesseract
from PIL import Image, ImageOps, ImageFilter

logger = logging.getLogger(__name__)


@dataclass
class OCRWord:
    text: str
    left: int
    top: int
    width: int
    height: int
    confidence: float | None = None
    coordinate_scale: float = 1.0


@dataclass
class OCRPage:
    page_number: int
    text: str
    image: Image.Image
    words: list[OCRWord] | None = None


@dataclass
class OCRResult:
    pages: list[OCRPage]
    used_ocr: bool


def _preprocess(image: Image.Image) -> Image.Image:
    image = ImageOps.exif_transpose(image).convert("RGB")
    # Keep enough resolution for small receipt text while limiting memory use.
    max_side = 2400
    scale = min(1.0, max_side / max(image.size))
    if scale < 1:
        image = image.resize((int(image.width * scale), int(image.height * scale)))
    gray = ImageOps.grayscale(image)
    gray = ImageOps.autocontrast(gray)
    return gray.filter(ImageFilter.SHARPEN)


def _orient(processed: Image.Image) -> Image.Image:
    try:
        osd = pytesseract.image_to_osd(processed)
        angle = int(next((line.split(": ", 1)[1] for line in osd.splitlines() if line.startswith("Rotate: ")), "0"))
        if angle:
            processed = processed.rotate(angle, expand=True)
    except Exception:
        # OSD is unreliable on very small/low-quality receipts; normal OCR is still useful.
        pass
    return processed


def _ocr_image_with_data(image: Image.Image) -> tuple[str, list[OCRWord]]:
    oriented = ImageOps.exif_transpose(image).convert("RGB")
    max_side = 2400
    scale = min(1.0, max_side / max(oriented.size))
    processed = _orient(_preprocess(image))
    data = pytesseract.image_to_data(processed, config="--psm 6", output_type=pytesseract.Output.DICT)
    words: list[OCRWord] = []
    text_lines: dict[tuple[int, int, int], list[str]] = {}
    n = len(data.get("text", []))
    for i in range(n):
        raw = str(data["text"][i] or "").strip()
        if not raw:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = None
        word = OCRWord(
            text=raw,
            left=int(data["left"][i]),
            top=int(data["top"][i]),
            width=int(data["width"][i]),
            height=int(data["height"][i]),
            confidence=conf,
            coordinate_scale=scale,
        )
        words.append(word)
        key = (int(data["block_num"][i]), int(data["par_num"][i]), int(data["line_num"][i]))
        text_lines.setdefault(key, []).append(raw)
    text = "\n".join(" ".join(parts) for _, parts in sorted(text_lines.items()))
    return text, words


def _ocr_image(image: Image.Image) -> str:
    return _ocr_image_with_data(image)[0]


def extract_text(content: bytes, filename: str) -> OCRResult:
    suffix = Path(filename).suffix.lower()
    pages: list[OCRPage] = []

    if suffix == ".pdf":
        doc = fitz.open(stream=content, filetype="pdf")
        used_ocr = False
        for idx, page in enumerate(doc):
            native_text = page.get_text("text").strip()
            pix = page.get_pixmap(dpi=180, alpha=False)
            image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            if len(native_text) >= 40:
                text = native_text
                # Native financial PDFs do not need OCR just to obtain their text.
                # Layout words are generated lazily only if invoice extraction needs
                # them, avoiding a second OCR pass over every native-text statement.
                words = None
            else:
                text, words = _ocr_image_with_data(image)
                used_ocr = True
            pages.append(OCRPage(idx + 1, text, image, words))
        doc.close()
        return OCRResult(pages, used_ocr)

    image = Image.open(io.BytesIO(content))
    text, words = _ocr_image_with_data(image)
    return OCRResult([OCRPage(1, text, image, words)], True)


def ensure_layout_words(page: OCRPage) -> list[OCRWord]:
    """Lazily compute coordinate-aware OCR for a page when a table needs it."""
    if page.words is None:
        _, page.words = _ocr_image_with_data(page.image)
    return page.words


def combined_text(result: OCRResult) -> str:
    return "\n\n".join(f"--- PAGE {p.page_number} ---\n{p.text}" for p in result.pages)
