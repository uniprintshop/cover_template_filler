"""Page-aware PDF extraction with bounded OCR and source provenance."""

import hashlib
import io
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber
import pytesseract
from pdf2image import convert_from_path
from PIL import Image
from pypdf import PdfReader

LOG = logging.getLogger("thesis-cover")


@dataclass(frozen=True)
class EvidenceBlock:
    page: int
    source: str
    text: str


@dataclass
class CoverEvidence:
    blocks: list[EvidenceBlock]
    source: str
    metadata: dict = field(default_factory=dict)
    timings: dict = field(default_factory=dict)

    @property
    def text(self):
        return "\n\n".join(block.text for block in self.blocks if block.text.strip())

    def __iter__(self):
        # Keep the public (text, source) unpacking API used by replay scripts.
        return iter((self.text, self.source))

    def prompt(self, max_chars=2400):
        digital = [b for b in self.blocks if b.source == "digital"]
        ocr = [b for b in self.blocks if b.source != "digital"]
        # Reserve most of the window for typed fields even on image-rich covers.
        image_budget = min(700, max_chars // 3) if digital else max_chars
        image_text = "\n".join(b.text for b in ocr)[:image_budget]
        digital_text = "\n\n".join(b.text for b in digital)
        if not digital:
            return image_text
        remaining = max_chars - len(image_text) - (2 if image_text else 0)
        return (image_text + "\n\n" if image_text else "") + digital_text[:max(0, remaining)]


def compact_cover(text: str) -> str:
    lines = []
    for line in text.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line or (lines and lines[-1] != ""):
            lines.append(line)
    return "\n".join(lines).strip()


def _logo_text_plausible(text: str) -> bool:
    letters = sum(ch.isalpha() for ch in text)
    return letters >= 4 and letters / max(1, len(text)) >= .5 and any(
        sum(ch.isalpha() for ch in word) >= 4 for word in text.split())


def _ocr(image, lang, timeout, config=""):
    started = time.monotonic()
    try:
        return pytesseract.image_to_string(image, lang=lang, config=config, timeout=timeout) or ""
    except pytesseract.TesseractError as exc:
        if lang == "eng" or "tessdata" not in str(exc).lower():
            raise
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise RuntimeError("OCR language fallback exceeded its time budget") from exc
        return pytesseract.image_to_string(image, lang="eng", config=config, timeout=remaining) or ""


def _digital_pages(path, pages):
    with pdfplumber.open(str(path)) as pdf:
        if not pdf.pages:
            raise RuntimeError(f"PDF has no pages: {path}")
        blocks = []
        for number, page in enumerate(pdf.pages[:pages], 1):
            text = page.extract_text() or ""
            if len(text.strip()) < 40:
                text = page.extract_text(layout=True) or text
            blocks.append(EvidenceBlock(number, "digital", compact_cover(text)))
        return blocks


def _image_blocks(path, pages, cfg, reader=None):
    blocks = []
    deadline = time.monotonic() + float(cfg.get("ocr_budget_seconds", 20))
    max_images = int(cfg.get("ocr_image_limit", 12))
    edge = int(cfg.get("ocr_image_max_edge", 1600))
    seen = set()
    attempted = 0
    try:
        reader = reader or PdfReader(str(path))
        for number, page in enumerate(reader.pages[:pages], 1):
            for entry in page.images:
                remaining = deadline - time.monotonic()
                if attempted >= max_images or remaining <= 0:
                    LOG.warning("Embedded-image OCR budget reached for %s", path.name)
                    return blocks
                try:
                    data = entry.data
                    digest = hashlib.sha256(data).digest()
                    if digest in seen:
                        continue
                    seen.add(digest)
                    with Image.open(io.BytesIO(data)) as image:
                        if image.width < 60 or image.height < 15:
                            continue
                        attempted += 1
                        image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return blocks
                        text = _ocr(image, cfg.get("ocr_lang", "deu+eng"),
                                    min(float(cfg.get("ocr_timeout_seconds", 8)), remaining), "--psm 11")
                    text = compact_cover(text)
                    if _logo_text_plausible(text):
                        blocks.append(EvidenceBlock(number, "embedded_ocr", text))
                except (OSError, ValueError, RuntimeError, Image.DecompressionBombError) as exc:
                    LOG.warning("Skipping unreadable cover image on page %s: %s", number, exc)
    except Exception as exc:
        LOG.warning("Embedded-image extraction unavailable: %s", exc)
    return blocks


def extract_page_image_text(pdf_path: Path, pages=2, cfg=None) -> str:
    return "\n".join(b.text for b in _image_blocks(pdf_path, pages, cfg or {}))


def extract_digital(pdf_path: Path, pages=2) -> str:
    digital = "\n\n".join(b.text for b in _digital_pages(pdf_path, pages))
    images = extract_page_image_text(pdf_path, pages)
    return (images + "\n\n" + digital).strip()


def _full_page_blocks(path, pages, dpi, lang, cfg):
    deadline = time.monotonic() + float(cfg.get("ocr_budget_seconds", 20))
    images = convert_from_path(str(path), dpi=dpi, first_page=1, last_page=pages,
                               timeout=min(float(cfg.get("ocr_render_timeout_seconds", 15)),
                                           float(cfg.get("ocr_budget_seconds", 20))))
    blocks = []
    try:
        for number, image in enumerate(images, 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Full-page OCR exceeded its time budget")
            text = _ocr(image, lang, min(float(cfg.get("ocr_timeout_seconds", 8)), remaining))
            blocks.append(EvidenceBlock(number, "ocr", compact_cover(text)))
    finally:
        for image in images:
            image.close()
    return blocks


def extract_ocr(pdf_path: Path, pages: int, dpi: int, lang: str, cfg=None, *, return_evidence=False):
    blocks = _full_page_blocks(pdf_path, pages, dpi, lang, cfg or {})
    return CoverEvidence(blocks, "ocr") if return_evidence else "\n\n".join(b.text for b in blocks)


def pdf_metadata(pdf_path: Path, reader=None) -> dict:
    info = (reader or PdfReader(str(pdf_path))).metadata
    if not info:
        return {}
    created = str(getattr(info, "creation_date", "") or "")
    result = {"author": getattr(info, "author", None), "title": getattr(info, "title", None),
              "created": created}
    years = sorted(set(re.findall(r"\b(?:19|20)\d{2}\b", created)))
    if years:
        result["meta_years"] = years
    return result


def get_cover_text(pdf_path: Path, cfg: dict) -> CoverEvidence:
    started = time.monotonic()
    reader = PdfReader(str(pdf_path))
    digital = _digital_pages(pdf_path, 2)
    digital_seconds = time.monotonic() - started
    image_start = time.monotonic()
    images = _image_blocks(pdf_path, 2, cfg, reader)
    timings = {"digital_seconds": digital_seconds, "image_ocr_seconds": time.monotonic() - image_start}
    if sum(len(b.text.strip()) for b in digital) >= int(cfg.get("ocr_min_chars", 80)):
        blocks = images + digital
        source = "digital+images" if images else "digital"
    else:
        ocr_start = time.monotonic()
        blocks = _full_page_blocks(pdf_path, 2, int(cfg.get("ocr_dpi", 300)), cfg.get("ocr_lang", "deu+eng"), cfg)
        timings["full_page_ocr_seconds"] = time.monotonic() - ocr_start
        source = "ocr"
    return CoverEvidence(blocks, source, pdf_metadata(pdf_path, reader), timings)
