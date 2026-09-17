#!/usr/bin/env python3
"""
Watch a folder for new thesis PDFs, read the first two pages (OCR if needed),
ask a local model to pick verbatim fields, fill the matching .upf cover, save it.

Usage:
  python thesis_cover_watch.py --config config.json
  python thesis_cover_watch.py --config config.json --once /path/to/file.pdf
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

try:
    from pdf2image import convert_from_path
except ImportError:
    convert_from_path = None

try:
    import pytesseract
except ImportError:
    pytesseract = None

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:
    FileSystemEventHandler = object  # type: ignore
    Observer = None

try:
    from watchdog.observers.polling import PollingObserver
except ImportError:
    PollingObserver = None


LOG = logging.getLogger("thesis-cover")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s).strip().lower()


def load_config(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    data["_config_path"] = str(path.resolve())
    return data


def persist_learned_template(
    cfg: dict,
    uni_id: str,
    template_name: str,
    extra_aliases: list[str],
) -> None:
    """Write the model's .upf choice into universities[].template for next time."""
    unis = cfg.setdefault("universities", [])
    found = None
    for u in unis:
        if u.get("id") == uni_id:
            u["template"] = template_name
            found = u
            break
    if found is None:
        aliases: list[str] = []
        seen = set()
        for a in extra_aliases:
            k = norm(str(a))
            if k and k not in seen:
                seen.add(k)
                aliases.append(str(a).strip())
        unis.append({"id": uni_id, "aliases": aliases, "template": template_name})
    path = cfg.get("_config_path")
    if not path:
        LOG.warning("Learned %s for %s but config path is unknown — not saved", template_name, uni_id)
        return
    on_disk = json.loads(Path(path).read_text(encoding="utf-8"))
    on_disk["universities"] = unis
    Path(path).write_text(json.dumps(on_disk, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    LOG.info("Remembered template %s for %s in %s", template_name, uni_id, path)


def ensure_dirs(*paths: Path) -> None:
    for p in paths:
        p.mkdir(parents=True, exist_ok=True)


def retry_io(fn, *, what: str, attempts: int = 4, delay: float = 5.0):
    """Run a file/mount operation with retries.

    GVFS/AFP mounts occasionally drop for a few seconds; a write that races
    with such a dropout raises FileNotFoundError/OSError. Retry with backoff
    so a share blip doesn't turn a finished job into a FAIL report.
    """
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except (OSError, FileNotFoundError) as exc:
            last = exc
            if i < attempts - 1:
                LOG.warning("%s failed (%s) — retry %s/%s in %ss", what, exc, i + 1, attempts - 1, delay)
                time.sleep(delay)
    raise last


def wait_until_stable(path: Path, settle: float) -> bool:
    last = -1
    stable_since = None
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False
        if size == last and size > 0:
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since >= settle:
                return True
        else:
            stable_since = None
            last = size
        time.sleep(0.2)
    return path.exists() and path.stat().st_size > 0


# ---------------------------------------------------------------------------
# PDF text
# ---------------------------------------------------------------------------

def extract_digital(pdf_path: Path, pages: int = 2) -> str:
    parts: list[str] = []
    if pdfplumber is not None:
        with pdfplumber.open(str(pdf_path)) as pdf:
            if not pdf.pages:
                raise RuntimeError(f"PDF has no pages: {pdf_path}")
            for page in pdf.pages[:pages]:
                t = page.extract_text() or ""
                if len(t.strip()) < 40:
                    t = page.extract_text(layout=True) or t
                parts.append(t or "")
    elif PdfReader is not None:
        reader = PdfReader(str(pdf_path))
        if not reader.pages:
            raise RuntimeError(f"PDF has no pages: {pdf_path}")
        for page in reader.pages[:pages]:
            parts.append(page.extract_text() or "")
    else:
        raise RuntimeError("Install pdfplumber or pypdf")
    text = "\n\n".join(parts)
    images_note = extract_page_image_text(pdf_path, pages)
    if images_note:
        text = (text.rstrip() + "\n\n" + images_note).strip()
    return text


def _logo_text_plausible(txt: str) -> bool:
    """Reject OCR garbage (single letters, symbols) from tiny logo images."""
    letters = [ch for ch in txt if ch.isalpha()]
    if len(letters) < 4:
        return False
    if len(txt) > 0 and len(letters) / len(txt) < 0.5:
        return False
    words = [w for w in re.split(r"\s+", txt) if w]
    if not any(sum(1 for c in w if c.isalpha()) >= 4 for w in words):
        return False
    return True


def extract_page_image_text(pdf_path: Path, pages: int = 2) -> str:
    """OCR embedded raster images (logos) on the first pages.

    Vector text extraction misses the university logo, which is often the
    only place naming the school. Re-rendering whole pages is slow and
    fragile on network shares; embedded images are small and fast.
    """
    if pytesseract is None:
        return ""
    if PdfReader is None:
        return ""
    try:
        from PIL import Image
    except ImportError:
        return ""
    try:
        reader = PdfReader(str(pdf_path))
    except Exception:
        return ""
    found: list[str] = []
    for page in reader.pages[:pages]:
        try:
            images = list(page.images)
        except Exception:
            continue
        for img in images:
            try:
                data = img.data
            except Exception:
                continue
            if len(data) < 2000:
                continue
            try:
                import io
                pil = Image.open(io.BytesIO(data))
            except Exception:
                continue
            w, h = pil.size
            if w < 60 or h < 15 or w * h > 2_000_000:
                continue
            try:
                txt = pytesseract.image_to_string(pil, lang="eng", config="--psm 11") or ""
            except Exception:
                continue
            txt = " ".join(txt.split())
            if len(txt.strip()) >= 3 and _logo_text_plausible(txt):
                found.append(txt.strip())
    seen, out = set(), []
    for f in found:
        if f.lower() not in seen:
            seen.add(f.lower())
            out.append(f)
    return "\n".join(out)


def extract_ocr(pdf_path: Path, pages: int, dpi: int, lang: str) -> str:
    if convert_from_path is None or pytesseract is None:
        raise RuntimeError("OCR requested but pdf2image/pytesseract not installed")
    try:
        images = convert_from_path(
            str(pdf_path),
            dpi=dpi,
            first_page=1,
            last_page=pages,
        )
    except Exception as exc:
        raise RuntimeError(f"Could not rasterize {pdf_path} for OCR ({exc})") from exc
    texts: list[str] = []
    for img in images:
        try:
            texts.append(pytesseract.image_to_string(img, lang=lang) or "")
        except Exception as exc:
            if lang != "eng" and "tessdata" in str(exc).lower():
                texts.append(pytesseract.image_to_string(img, lang="eng") or "")
            else:
                raise RuntimeError(
                    f"Tesseract failed on {pdf_path} ({exc}). "
                    "Install tesseract-data-deu tesseract-data-eng."
                ) from exc
    return "\n\n".join(texts)


def compact_cover(text: str) -> str:
    """Collapse layout-extraction padding so aliases, names and the model window stay usable."""
    lines = []
    for line in text.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line:
            lines.append(line)
        elif lines and lines[-1] != "":
            lines.append("")
    return "\n".join(lines).strip()


def get_cover_text(pdf_path: Path, cfg: dict) -> tuple[str, str]:
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    digital = compact_cover(extract_digital(pdf_path, 2))
    if len(digital.strip()) >= int(cfg.get("ocr_min_chars", 80)):
        return digital, "digital"
    LOG.info("Digital text too short (%s chars) — running OCR", len(digital.strip()))
    ocr = compact_cover(
        extract_ocr(
            pdf_path,
            pages=2,
            dpi=int(cfg.get("ocr_dpi", 300)),
            lang=cfg.get("ocr_lang", "deu+eng"),
        )
    )
    return ocr, "ocr"


def pdf_metadata(pdf_path: Path) -> dict:
    if PdfReader is None:
        return {}
    info = PdfReader(str(pdf_path)).metadata
    if not info:
        return {}
    author = getattr(info, "author", None)
    title = getattr(info, "title", None)
    created = str(getattr(info, "creation_date", "") or "")
    out = {
        "author": author,
        "title": title,
        "created": created,
    }
    # Word/LibreOffice embeds a usable author + creation year in metadata.
    # It is only trusted when the author string also appears verbatim on the
    # cover (same person named in the PDF) — never as a standalone source.
    meta_years = sorted(set(YEAR_RE.findall(created)))
    if meta_years:
        out["meta_years"] = meta_years
    return out


# ---------------------------------------------------------------------------
# heuristic candidates (model must copy from these / cover text)
# ---------------------------------------------------------------------------

# Longest phrases first — we copy the exact substring from the PDF.
DEGREE_PHRASES = [
    "Bachelor's Thesis",
    "Master's Thesis",
    "Doctoral Thesis",
    "Bachelor-Thesis",
    "Master-Thesis",
    "Bachelorarbeit",
    "Masterarbeit",
    "Projektarbeit",
    "Dissertation",
    "Doktorarbeit",
    "Promotionsarbeit",
    "Bachelor Thesis",
    "Master Thesis",
    "PhD Thesis",
    "Ph.D. Thesis",
]

DEGREE_PATTERNS = [
    (r"\b(ph\.?\s*d\.?|doktorarbeit|dissertation|doctoral\s+thesis|promotionsarbeit)\b", "dissertation"),
    (r"\b(master'?s?\s+thesis|masterarbeit|master-thesis|m\.?\s*sc\.?|m\.?\s*a\.?|magister)\b", "master"),
    (r"\b(bachelor'?s?\s+thesis|bachelorarbeit|bachelor-thesis|projektarbeit|b\.?\s*sc\.?|b\.?\s*a\.?)\b", "bachelor"),
]

YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
AUTHOR_CUE = re.compile(
    r"(?i)(?P<cue>vorgelegt\s+von|eingereicht\s+von|vorgelegt\s+dem|submitted\s+by|"
    r"verfasser(?:in)?|autor(?:in)?|written\s+by|author|\bvon\b)"
    r"\s*[:\-–]?\s*"
    r"(?P<name>[A-ZÄÖÜ][\wÄÖÜäöüß.'\-]+(?:[ \t]+[A-ZÄÖÜ][\wÄÖÜäöüß.'\-]+){1,5})"
)
SUPERVISOR_ROLE = re.compile(
    r"(?i)\b(erstgutachter(?:in)?|zweitgutachter(?:in)?|gutachter(?:in)?|"
    r"betreuer(?:in)?|supervisor|examiner)\b"
)
SUPERVISOR_TITLE = re.compile(r"(?i)^prof\.?\b")
GENERIC_TEMPLATE_TOKENS = {
    "muster", "logo", "uni", "und", "der", "die", "das", "the", "new",
    "neues", "altes", "neu", "alt", "cover", "template", "hs", "fh",
    "gross", "groß", "klein", "rund", "din", "campus", "neueslogo",
    "universitat", "universität", "university", "hochschule", "technische",
    "fachhochschule", "schule",
    # Generic words that appear inside compound words / faculty names on
    # almost every cover ("AutomatisierungsTECHNIK", "INSTITUT für ...",
    # "Fakultät für ..."). They must never count as a template match.
    "fur", "für", "technik", "institut", "institute", "wirtschaft",
    "gesundheit", "management", "ist", "fakultat", "fakultät",
    "fakultaet", "laboratorium", "labor", "prof", "ing", "dr",
    "abteilung", "fachbereich", "lehrstuhl",
}


def guess_degree(text: str) -> str:
    low = text.lower()
    for pat, label in DEGREE_PATTERNS:
        if re.search(pat, low):
            return label
    return "unknown"


def exact_degree_phrase(text: str) -> str:
    """Return the degree wording exactly as it appears in the PDF."""
    best = ""
    low = text.lower()
    for phrase in DEGREE_PHRASES:
        i = low.find(phrase.lower())
        if i >= 0:
            exact = text[i : i + len(phrase)]
            if len(exact) > len(best):
                best = exact
    return best


def years_in(text: str) -> list[str]:
    seen, out = set(), []
    for m in YEAR_RE.finditer(text):
        y = m.group(1)
        if y not in seen:
            seen.add(y)
            out.append(y)
    return out


def _unique_names(names: list[str]) -> list[str]:
    seen, out = set(), []
    for c in names:
        k = c.lower()
        if 3 <= len(c) <= 80 and k not in seen:
            seen.add(k)
            out.append(c)
    return out


def _cue_is_strong(cue: str) -> bool:
    """True for a candidate field; False for citation-like 'written by' / bare 'author'."""
    c = re.sub(r"\s+", " ", cue.strip().lower())
    if c in {"written by", "author"}:
        return False
    # Bare "von" is only a cue when it stands alone on a line (the classic
    # German cover layout: "...\nvon\n<Name>"). Inline "von ..." inside
    # running text (e.g. a bibliography entry "... von Wohngebäuden") is not.
    return True


def labelled_author_groups(text: str) -> tuple[list[str], list[str]]:
    """Split cover name cues into candidate fields vs weak/citation cues."""
    strong, weak = [], []
    for m in AUTHOR_CUE.finditer(text):
        cue = m.group("cue")
        name = m.group("name").strip()
        if cue.strip().lower() == "von" and not _von_stands_alone(text, m.start("cue")):
            continue
        if NON_PERSON_WORDS.search(name):
            continue
        if _cue_is_strong(cue):
            strong.append(name)
        else:
            weak.append(name)
    # Name standing directly above the matriculation number is the candidate,
    # e.g. "Annamaria Maximiliane Droste\nMatrikelnummer: 4231991".
    for m in MATRIKEL_NAME_RE.finditer(text):
        name = m.group("name").strip()
        if NON_PERSON_WORDS.search(name):
            continue
        if not looks_like_supervisor(name):
            strong.append(name)
    return _unique_names(strong), _unique_names(weak)


NON_PERSON_WORDS = re.compile(
    r"(?i)\b(laboratorium|labor|institut|fakultät|fakultat|fakultaet|"
    r"universität|universitat|hochschule|technik|technologie|abteilung|"
    r"fachbereich|lehrstuhl|regelungstechnik|prozessleittechnik|"
    r"automatisierungstechnik|elektrotechnik|anlagen|klinik|zentrum)\b"
)
MATRIKEL_NAME_RE = re.compile(
    r"(?m)^(?P<name>[A-ZÄÖÜ][\wÄÖÜäöüß.'\-]+(?:[ \t]+[A-ZÄÖÜ][\wÄÖÜäöüß.'\-]+){1,4})[ \t]*\r?$"
    r"\n[ \t]*Matrikel(?:nummer|nr)?\.?\s*:?"
)


def _von_stands_alone(text: str, pos: int) -> bool:
    """True if 'von' at pos is alone on its line (classic cover layout)."""
    line_start = text.rfind("\n", 0, pos) + 1
    line_end = text.find("\n", pos)
    if line_end < 0:
        line_end = len(text)
    return text[line_start:line_end].strip().lower() == "von"


def author_hints(text: str, meta: dict | None = None) -> list[str]:
    """Labelled authors on the cover only. PDF metadata is untrusted. Strong cues first."""
    del meta
    strong, weak = labelled_author_groups(text)
    return _unique_names(strong + weak)


def _alias_in_blob(alias: str, blob: str) -> bool:
    a = norm(alias)
    if not a:
        return False
    if len(a) <= 4:
        return re.search(rf"(?:^|\s){re.escape(a)}(?:\s|$)", blob) is not None
    # Multi-word aliases: every word must appear as a whole word, so
    # "TH Köln" does not match inside "AutomatisierungsTECHNIK ..."
    # and "Institut" does not match inside "INSTITUT für ...".
    if " " in a:
        whole = all(
            re.search(rf"(?:^|[\s,.;:()\-–—/]){re.escape(w)}(?:$|[\s,.;:()\-–—/])", blob) is not None
            for w in a.split()
        )
        if whole:
            return True
        # Glued-text fallback (PDFs whose text layer has no spaces, e.g.
        # "derBergischenUniversitätWuppertal"): the alias must appear with
        # all spaces removed, as one contiguous run, to still count.
        compact_blob = re.sub(r"[\s,.;:()\-–—/]+", "", blob)
        compact_a = "".join(a.split())
        return compact_a in compact_blob
    # Single long token: still require word boundaries so "technik" does
    # not match inside "automatisierungstechnik". Glued-text fallback for
    # long tokens: a city/school name like "wuppertal" may be embedded in
    # a glued compound ("...universitätwuppertal") with no space before it.
    if re.search(rf"(?:^|[^\wäöüß]){re.escape(a)}(?:$|[^\wäöüß])", blob) is not None:
        return True
    return len(a) >= 7 and a in re.sub(r"\s+", "", blob)


def _uni_aliases(uni: dict) -> list[str]:
    aliases = list(uni.get("aliases", []))
    aliases.append(uni.get("id", ""))
    aliases.append(Path(uni.get("template", "")).stem)
    return [a for a in aliases if a]


def cover_university_hits(text: str, universities: list[dict]) -> list[dict]:
    """Every config university whose alias actually appears on the cover."""
    blob = norm(text)
    hits: list[dict] = []
    for uni in universities:
        best_alias = ""
        best_len = 0
        for alias in _uni_aliases(uni):
            if _alias_in_blob(alias, blob) and len(norm(alias)) >= best_len:
                best_alias = alias
                best_len = len(norm(alias))
        if best_alias:
            hits.append({"uni": uni, "alias": best_alias, "alias_len": best_len})
    return hits


def _full_template_name_in_blob(stem: str, cover_text: str) -> bool:
    """True if the template's full identifying name appears on the cover.

    Strips generic/title filler (Muster, TH, HS, ...) and requires every
    remaining word to match as a whole word. E.g. "TH Köln" needs both
    "th" AND "köln"; "Technik" inside "Automatisierungstechnik" is not enough.
    """
    blob = norm(cover_text)
    filler = set(GENERIC_TEMPLATE_TOKENS) | {"th", "hs", "fh", "tu", "fh", "rwth", "hsh", "hdm"}
    words = [w for w in re.split(r"[^\wäöüß]+", norm(stem)) if w and w not in filler]
    if not words:
        return False
    if all(
        re.search(rf"(?:^|[^\wäöüß]){re.escape(w)}(?:$|[^\wäöüß])", blob) is not None
        for w in words
    ):
        return True
    # Glued-text fallback: PDF text layers sometimes drop all spaces
    # ("derBergischenUniversitätWuppertal"). Then the identifying words must
    # appear glued together, in order, as one run.
    compact_blob = re.sub(r"[\s,.;:()\-–—/]+", "", blob)
    compact_words = "".join(words)
    return compact_words in compact_blob


def filename_university_hits(text: str, templates_dir: Path | None) -> list[dict]:
    if not templates_dir or not templates_dir.is_dir():
        return []
    blob = norm(text)
    scored: list[tuple[int, Path]] = []
    for path in templates_dir.glob("*.upf"):
        tokens = [
            tok for tok in re.split(r"[\s_\-–.]+", path.stem)
            if len(tok) >= 3 and norm(tok) not in GENERIC_TEMPLATE_TOKENS
        ]
        if not tokens:
            continue
        hits = sum(1 for tok in tokens if _alias_in_blob(tok, blob))
        if hits and hits >= max(1, (len(tokens) + 1) // 2):
            scored.append((hits, path))
    if not scored:
        return []
    top = max(s for s, _ in scored)
    winners = [p for s, p in scored if s == top]
    return [
        {"uni": {"id": p.stem, "aliases": [p.stem], "template": p.name}, "alias": p.stem, "alias_len": len(p.stem)}
        for p in winners
    ]


def _first_hit_on_cover(cover_text: str, hits: list[dict]) -> dict | None:
    blob = norm(cover_text)
    best = None
    best_pos = None
    for h in hits:
        for alias in _uni_aliases(h["uni"]):
            a = norm(alias)
            if not a or not _alias_in_blob(alias, blob):
                continue
            pos = blob.find(a) if len(a) > 4 else (re.search(rf"(?:^|\s){re.escape(a)}(?:\s|$)", blob).start() if re.search(rf"(?:^|\s){re.escape(a)}(?:\s|$)", blob) else -1)
            if pos >= 0 and (best_pos is None or pos < best_pos):
                best_pos = pos
                best = h
    return best


def _model_picks_hit(model_guess: str, hits: list[dict], cover_text: str) -> dict | None:
    """Allow the model to choose only among universities already on the cover."""
    if not model_guess or not hits:
        return None
    ng = norm(model_guess)
    if not ng:
        return None
    matching: list[dict] = []
    for h in hits:
        names = _uni_aliases(h["uni"])
        if any(norm(a) and (norm(a) in ng or ng in norm(a)) for a in names):
            matching.append(h)
    ids = {h["uni"]["id"]: h for h in matching}
    if len(ids) == 1:
        return next(iter(ids.values()))["uni"]
    if len(ids) > 1:
        # Guess named several cover schools (typical Kooperation line). Letterhead = first alias on the page.
        first = _first_hit_on_cover(cover_text, list(ids.values()))
        return first["uni"] if first else None
    return None


def resolve_university(
    cover_text: str,
    model_guess: str,
    universities: list[dict],
    templates_dir: Path | None,
) -> tuple[dict | None, list[str], list[dict]]:
    risks: list[str] = []
    hits = cover_university_hits(cover_text, universities)
    source = "alias"
    if not hits:
        hits = filename_university_hits(cover_text, templates_dir)
        source = "filename"
        if hits:
            # Filename-only hits are weak evidence (a city/word in a compound
            # name). Only trust a template whose full identifying part matches,
            # e.g. "TH Köln" as whole words — never a lone "Technik"/"Institut".
            strong = [
                h for h in hits
                if _full_template_name_in_blob(Path(h["uni"]["template"]).stem, cover_text)
            ]
            if strong:
                hits = strong
            else:
                hits = []
                risks.append("weak_template_filename_ignored")

    summary = [
        {"id": h["uni"]["id"], "template": h["uni"].get("template", ""), "alias": h["alias"]}
        for h in hits
    ]
    if not hits:
        return None, ["no_university_on_cover"] + risks, summary

    by_id = {h["uni"]["id"]: h for h in hits}
    if len(by_id) == 1:
        if source == "filename":
            risks.append("university_from_template_filename")
        return next(iter(by_id.values()))["uni"], risks, summary

    picked = _model_picks_hit(model_guess, list(by_id.values()), cover_text)
    if picked:
        risks.append("university_disambiguated_by_model")
        return picked, risks, summary

    return None, [f"ambiguous_university:{','.join(sorted(by_id))}"], summary


def match_university(
    text: str,
    universities: list[dict],
    model_guess: str = "",
    templates_dir: Path | None = None,
) -> dict | None:
    uni, _risks, _hits = resolve_university(text, model_guess, universities, templates_dir)
    return uni


def distinctive_filename_tokens(name: str) -> list[str]:
    stem = Path(name).stem
    stem = re.sub(r"^\d+-", "", stem)
    stem = TITLE_NAME_RE.sub("", stem)
    tokens: list[str] = []
    for tok in re.split(r"[\s_\-–./]+", stem):
        n = norm(tok)
        if not n or n in GENERIC_TEMPLATE_TOKENS:
            continue
        if len(n) >= 4 or (len(n) >= 2 and tok.isupper()):
            tokens.append(tok)
    return tokens


def list_plain_templates(templates_dir: Path | None) -> list[Path]:
    if not templates_dir or not templates_dir.is_dir():
        return []
    return sorted(
        p for p in templates_dir.glob("*.upf") if p.is_file() and not is_title_template(p.name)
    )


def grounded_templates(
    cover_text: str,
    templates_dir: Path | None,
    universities: list[dict] | None = None,
) -> list[Path]:
    """Plain .upf files whose names (or config aliases) are actually on the cover."""
    catalog = list_plain_templates(templates_dir)
    if not catalog:
        return []
    blob = norm(cover_text)
    chosen: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        key = str(path.resolve())
        if key not in seen and path.is_file():
            seen.add(key)
            chosen.append(path)

    for path in catalog:
        if not distinctive_filename_tokens(path.name):
            continue
        if _full_template_name_in_blob(path.stem, cover_text):
            add(path)

    for uni in universities or []:
        if not any(_alias_in_blob(a, blob) for a in _uni_aliases(uni)):
            continue
        configured = uni.get("template") or ""
        if configured and templates_dir:
            add(templates_dir / configured)
        needle = " ".join(_uni_aliases(uni))
        for path in catalog:
            if any(_alias_in_blob(tok, norm(needle)) for tok in distinctive_filename_tokens(path.name)):
                add(path)
    return chosen


def _match_template_guess(guess: str, shortlist: list[Path]) -> Path | None:
    if not guess or not shortlist:
        return None
    g = norm(Path(str(guess).strip()).name)
    if not g:
        return None
    exact = [p for p in shortlist if norm(p.name) == g]
    if len(exact) == 1:
        return exact[0]
    family = template_family(str(guess))
    fam = [p for p in shortlist if template_family(p.name) == family]
    if len(fam) == 1:
        return fam[0]
    if len(fam) > 1:
        numbered = [p for p in fam if re.match(r"^\d+-", p.name)]
        if numbered:
            return sorted(numbered, key=lambda p: p.name, reverse=True)[0]
        return sorted(fam, key=lambda p: p.name)[0]
    return None


def resolve_template(
    cover_text: str,
    model_template: str,
    templates_dir: Path | None,
    universities: list[dict] | None = None,
) -> tuple[Path | None, list[str], list[str]]:
    """Model may pick only among cover-grounded files in templates_dir."""
    risks: list[str] = []
    shortlist = grounded_templates(cover_text, templates_dir, universities)
    names = [p.name for p in shortlist]
    if not shortlist:
        return None, ["no_template_on_cover"], names
    picked = _match_template_guess(model_template, shortlist)
    if picked:
        if len(shortlist) > 1:
            risks.append("template_chosen_by_model")
        return picked, risks, names
    if len(shortlist) == 1:
        return shortlist[0], risks, names
    return None, [f"ambiguous_template:{len(shortlist)}"], names


def model_search_templates(
    cover_text: str,
    meta: dict,
    cfg: dict,
    uni_guess: str,
    author: str,
    degree_type: str,
) -> tuple[Path | None, str]:
    """Ask the model to pick from the whole template library.

    Used when no template filename matched the cover text. Returns the
    picked template path (or None) plus a human-readable note for NOTES.txt.
    The pick is still validated: the file must exist and not be a TITLE file.
    """
    templates_dir = Path(cfg["templates_dir"]) if cfg.get("templates_dir") else None
    if templates_dir is None or not templates_dir.is_dir():
        return None, ""
    mcfg = cfg.get("model", {})
    if mcfg.get("backend", "ollama") == "none":
        return None, "Model-template search skipped (backend=none)."
    catalog = sorted(
        p.name for p in templates_dir.glob("*.upf")
        if p.is_file() and not is_title_template(p.name)
    )
    if not catalog:
        return None, ""
    user = (
        "METADATA is untrusted; use only COVER_TEXT clues.\n"
        f"METADATA: {json.dumps(meta, ensure_ascii=False)}\n"
        f"EXTRACTED university={uni_guess!r} author={author!r} degree_type={degree_type!r}\n"
        f"AVAILABLE_TEMPLATES ({len(catalog)} files, one per line):\n"
        + "\n".join(catalog)
        + f"\nCOVER_TEXT:\n{cover_text_for_model(cover_text)}\n"
    )
    try:
        if mcfg.get("backend", "ollama") == "ollama":
            raw = call_ollama(
                mcfg.get("name", "qwen3:8b"),
                TEMPLATE_SEARCH_SYSTEM,
                user,
                mcfg.get("host", "http://[IP_ADDRESS]:11434"),
                min(int(mcfg.get("timeout", 180)), 60),
                think=model_think_value(mcfg),
                num_ctx=int(mcfg.get("num_ctx", 8192)),
                keep_alive=str(mcfg.get("keep_alive", "30m")),
                num_predict=150,
            )
        else:
            raw = call_openai(
                mcfg.get("name", "qwen3:8b"),
                TEMPLATE_SEARCH_SYSTEM,
                user,
                mcfg.get("openai_base", "http://[IP_ADDRESS]:1234/v1"),
                mcfg.get("api_key", "not-needed"),
                min(int(mcfg.get("timeout", 180)), 60),
            )
    except Exception as exc:
        return None, f"Model-template search failed ({exc})."
    data = parse_json_object(raw)
    guess = str(data.get("template") or "").strip()
    uni = str(data.get("university") or "").strip()
    reason = str(data.get("reason") or "").strip()
    if not guess:
        return None, (
            "Model-template search: the model found no plausible template "
            f"in the library{(f' ({reason})' if reason else '')}."
        )
    candidate = templates_dir / Path(guess).name
    if not candidate.is_file():
        return None, (
            f"Model-template search: model suggested {guess!r}, "
            "but that file does not exist — ignored."
        )
    if is_title_template(candidate.name):
        return None, (
            f"Model-template search: model suggested {guess!r}, "
            "but that is a TITEL/TITLE variant — ignored."
        )
    note = (
        f"Model-template search: no template name matched the cover text, "
        f"so the model searched the full library ({len(catalog)} files) and picked "
        f"{candidate.name!r}"
        + (f" for university {uni!r}" if uni else "")
        + (f" — {reason}" if reason else "")
        + "."
    )
    return candidate, note


def hardcoded_template_path(uni: dict | None, templates_dir: Path | None) -> Path | None:
    if not uni or not templates_dir:
        return None
    name = (uni.get("template") or "").strip()
    if not name:
        return None
    path = templates_dir / name
    return path if path.is_file() else None


def looks_like_supervisor(name: str) -> bool:
    if SUPERVISOR_ROLE.search(name):
        return True
    return SUPERVISOR_TITLE.match(name.strip()) is not None


def _name_consistent(a: str, b: str) -> bool:
    return bool(a and b) and (a == b or a in b or b in a)


def _prefer_longer(model: str, hint: str) -> str:
    return model if len(model) >= len(hint) else hint


def resolve_author(cover_text: str, model_author: str, labelled: list[str]) -> tuple[str, list[str]]:
    """Candidate field (vorgelegt von, …) beats citations; model may choose among field hits."""
    del labelled
    risks: list[str] = []
    model_ok = model_author if verbatim_ok(model_author, cover_text) else ""
    strong, weak = labelled_author_groups(cover_text)

    pick = ""
    if strong:
        matched = next((h for h in strong if _name_consistent(model_ok, h)), None)
        if matched:
            pick = _prefer_longer(model_ok, matched)
        else:
            pick = strong[0]
            if model_ok:
                risks.append("author_model_disagreed_used_labelled")
    elif model_ok:
        pick = model_ok
        risks.append("author_from_model_span")
        if weak:
            risks.append("author_ignored_weak_label")
    elif weak:
        pick = weak[0]

    if pick and looks_like_supervisor(pick):
        risks.append("author_looks_like_supervisor")
    return pick, risks


def year_near_author(cover_text: str, author: str, years: list[str]) -> str:
    if not author or author not in cover_text or not years:
        return ""
    idx = cover_text.find(author)
    window = cover_text[max(0, idx - 120) : idx + len(author) + 120]
    found = [y for y in years_in(window) if y in years]
    if len(found) == 1:
        return found[0]
    return ""


def year_from_submission_context(cover_text: str, years: list[str]) -> str:
    """Prefer a year next to Abgabe / city, not 'Beginn der Arbeit'."""
    for y in years:
        if re.search(
            rf"(?i)(?:abgabe|eingereicht|submitted|prüfung|erscheinen).{{0,48}}{y}",
            cover_text,
        ):
            return y
        if re.search(rf"(?i),\s*{y}\b", cover_text):
            return y
    return ""


def resolve_year(
    cover_text: str,
    model_year: str,
    years: list[str],
    author: str,
    meta_years: list[str] | None = None,
    publish_year: str = "",
) -> tuple[str, list[str]]:
    """The model picks the publishing year; it must still appear on the cover."""
    risks: list[str] = []
    my = (model_year or "").strip()
    if re.fullmatch(r"(19|20)\d{2}", my) and my in years:
        if len(years) > 1:
            risks.append("year_chosen_by_model")
        return my, risks
    if re.fullmatch(r"(19|20)\d{2}", my) and my in cover_text:
        risks.append("year_chosen_by_model")
        return my, risks
    if len(years) == 1:
        return years[0], risks
    near = year_near_author(cover_text, author, years)
    if near:
        risks.append("year_taken_near_author")
        return near, risks
    ctx = year_from_submission_context(cover_text, years)
    if ctx:
        risks.append("year_from_submission_context")
        return ctx, risks
    if years:
        return "", ["multiple_years_unresolved"]
    # No year anywhere on the cover: use the configured default, flagged.
    py = str(publish_year or "").strip()
    if re.fullmatch(r"(19|20)\d{2}", py):
        risks.append("year_from_publish_year_default")
        return py, risks
    # Last resort: the PDF creation year (Word metadata), clearly flagged.
    # The author still comes from the cover; only the missing year is filled.
    for y in meta_years or []:
        if re.fullmatch(r"(19|20)\d{2}", y):
            risks.append("year_from_pdf_metadata")
            return y, risks
    return "", []


# ---------------------------------------------------------------------------
# local model
# ---------------------------------------------------------------------------

SYSTEM = """You extract thesis cover fields from COVER_TEXT.
Reply with ONLY a JSON object, no markdown:
{"university":"","author":"","title":"","degree_phrase":"","degree_type":"bachelor|master|dissertation|unknown","year":"","template":""}
Rules:
- author, title, university, year, degree_phrase MUST be copied character-for-character from COVER_TEXT. Never from METADATA.
- The author is the candidate who submitted the work. Do not use Erstgutachter, Zweitgutachter, Betreuer, or supervisor names.
- The author line may stand alone with no "vorgelegt von" / "written by". Still copy that name exactly.
- If several universities appear, pick the awarding institution (letterhead), still copied verbatim.
- If several years appear, YOU decide which is the publishing/submission year (not a start date, copyright, or cited work). Copy that year exactly.
- degree_phrase is the exact wording on the cover (e.g. Bachelorarbeit, Master-Thesis, Projektarbeit). Do not rewrite it.
- If AVAILABLE_TEMPLATES is present, template MUST be copied exactly from that list (one filename). Pick the foil for the awarding university. Prefer the general university cover, not a Gymnasium, IHK, church, or partner school unless that body is clearly the issuer. Never pick a TITEL/TITLE file. If AVAILABLE_TEMPLATES is omitted, set template to "".
- Do not fix spelling. Do not translate. Do not invent.
- year must be a 4-digit year that appears in COVER_TEXT.
- degree_type is only bachelor, master, dissertation, or unknown.
- If a field is not present use "".
"""

TEMPLATE_SEARCH_SYSTEM = """You pick a foil-stamping cover template for a thesis.
Reply with ONLY a JSON object, no markdown:
{"template":"","university":"","reason":""}
Rules:
- template MUST be copied exactly (one filename) from AVAILABLE_TEMPLATES. Never invent a filename. Never pick a TITEL/TITLE file (they are not in the list).
- Use COVER_TEXT clues: university name, faculty, institute, city, logo text, department. Pick the template whose filename best matches the awarding institution.
- university is the awarding institution name copied character-for-character from COVER_TEXT if present, else "".
- reason is one short sentence saying which cover clue led to the pick.
- If nothing in the list plausibly matches, set template to "".
"""


def model_think_value(mcfg: dict) -> bool | str:
    """Ollama `think`: False disables reasoning; 'low'/'medium'/'high' if supported."""
    if "think" not in mcfg:
        return False
    v = mcfg["think"]
    if isinstance(v, str):
        low = v.strip().lower()
        if low in {"false", "0", "no", "off", "none", ""}:
            return False
        if low in {"true", "1", "yes", "on"}:
            return True
        return low
    return bool(v)


def call_ollama(
    model: str,
    system: str,
    user: str,
    host: str,
    timeout: int,
    *,
    json_mode: bool = True,
    num_predict: int = 300,
    think: bool | str = False,
    num_ctx: int = 8192,
    keep_alive: str = "30m",
) -> str:
    payload = {
        "model": model,
        "stream": False,
        "think": think,
        "keep_alive": keep_alive,
        "options": {"temperature": 0.0, "num_predict": num_predict, "num_ctx": num_ctx},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if json_mode:
        payload["format"] = "json"
    req = urllib.request.Request(
        host.rstrip("/") + "/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Ollama not reachable at {host} ({exc.reason})") from exc
    except TimeoutError as exc:
        raise RuntimeError(f"Ollama timed out after {timeout}s ({model})") from exc
    return body.get("message", {}).get("content", "") or body.get("error", "")


def call_openai(
    model: str,
    system: str,
    user: str,
    base: str,
    api_key: str,
    timeout: int,
    *,
    json_mode: bool = True,
    num_predict: int = 300,
) -> str:
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": num_predict,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OpenAI-compatible endpoint not reachable at {base} ({exc.reason})") from exc
    return body["choices"][0]["message"]["content"]


def cover_text_for_model(text: str, max_chars: int = 2400) -> str:
    """Trim cover text for the model prompt without losing cover fields.

    Pages 1-2 often include a table of contents ("Inhalt ....... II") whose
    dot-leader lines cost the model expensive prefill tokens but never hold
    author/university/year. Heuristics and verbatim validation still use the
    FULL text; only the model's view is trimmed.
    """
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if re.search(r"\.{4,}", stripped):  # dot leaders / ToC entries
            continue
        if re.fullmatch(r"(?i)(inhaltsverzeichnis|contents|table of contents)", stripped):
            continue
        lines.append(line)
    out = "\n".join(lines).strip()
    if len(out) > max_chars:
        out = out[:max_chars]
    return out


def parse_json_object(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, flags=re.S)
        if not m:
            return {}
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}
    return data if isinstance(data, dict) else {}


def verbatim_ok(value: str, haystack: str) -> bool:
    if not value:
        return False
    if value in haystack:
        return True
    compact_v = re.sub(r"\s+", " ", value).strip()
    compact_h = re.sub(r"\s+", " ", haystack)
    if compact_v and compact_v in compact_h:
        return True
    # Glued-text fallback: some PDF text layers drop ALL spaces
    # ("Vorgelegtvon:BrittaWenzel"). Compare with every space removed so the
    # model's properly-spaced copy ("Britta Wenzel") still validates.
    glue_v = re.sub(r"\s+", "", value)
    glue_h = re.sub(r"\s+", "", haystack)
    return bool(glue_v) and glue_v in glue_h


def cover_span(value: str, haystack: str) -> str:
    """Return a cover-grounded form of value, allowing wrapped-line titles."""
    if not value:
        return ""
    if value in haystack:
        return value
    compact_v = re.sub(r"\s+", " ", value).strip()
    compact_h = re.sub(r"\s+", " ", haystack)
    if compact_v and compact_v in compact_h:
        return compact_v
    # Glued-text fallback (see verbatim_ok): keep the model's spaced form.
    glue_v = re.sub(r"\s+", "", value)
    glue_h = re.sub(r"\s+", "", haystack)
    if glue_v and glue_v in glue_h:
        return compact_v
    return ""


def extract_fields(cover_text: str, meta: dict, cfg: dict) -> dict:
    years = years_in(cover_text)
    labelled_authors = author_hints(cover_text)
    degree = guess_degree(cover_text)
    candidates = {"authors": labelled_authors, "years": years, "degree_guess": degree}

    templates_dir = Path(cfg["templates_dir"]) if cfg.get("templates_dir") else None
    universities = cfg.get("universities", [])
    hardcoded_hits = [
        h for h in cover_university_hits(cover_text, universities)
        if hardcoded_template_path(h["uni"], templates_dir)
    ]
    need_model_template = not hardcoded_hits
    available_names = (
        [p.name for p in grounded_templates(cover_text, templates_dir, universities)]
        if need_model_template
        else []
    )
    user = (
        "METADATA is untrusted; copy only from COVER_TEXT.\n"
        f"METADATA: {json.dumps(meta, ensure_ascii=False)}\n"
        f"CANDIDATES: {json.dumps(candidates, ensure_ascii=False)}\n"
    )
    if need_model_template:
        user += f"AVAILABLE_TEMPLATES: {json.dumps(available_names, ensure_ascii=False)}\n"
    user += f"COVER_TEXT:\n{cover_text_for_model(cover_text)}\n"

    model_fields: dict = {}
    mcfg = cfg.get("model", {})
    backend = mcfg.get("backend", "ollama")
    if backend != "none":
        try:
            if backend == "ollama":
                raw = call_ollama(
                    mcfg.get("name", "qwen3:8b"),
                    SYSTEM,
                    user,
                    mcfg.get("host", "http://[IP_ADDRESS]:11434"),
                    int(mcfg.get("timeout", 180)),
                    think=model_think_value(mcfg),
                    num_ctx=int(mcfg.get("num_ctx", 8192)),
                    keep_alive=str(mcfg.get("keep_alive", "30m")),
                )
            else:
                raw = call_openai(
                    mcfg.get("name", "qwen3:8b"),
                    SYSTEM,
                    user,
                    mcfg.get("openai_base", "http://127.0.0.1:1234/v1"),
                    mcfg.get("api_key", "not-needed"),
                    int(mcfg.get("timeout", 180)),
                )
            model_fields = parse_json_object(raw)
            LOG.info("Model raw fields: %s", model_fields)
        except Exception as exc:
            LOG.warning("Model call failed (%s) — using heuristics only", exc)

    hay = cover_text
    risks: list[str] = []

    author, ar = resolve_author(
        hay, str(model_fields.get("author") or "").strip(), labelled_authors
    )
    risks.extend(ar)
    if author:
        author = cover_span(author, hay) or author

    title = cover_span(str(model_fields.get("title") or "").strip(), hay)

    year, yr = resolve_year(
        hay, str(model_fields.get("year") or "").strip(), years, author,
        list(meta.get("meta_years") or []),
        str(cfg.get("publish_year") or "").strip(),
    )
    risks.extend(yr)

    dt = str(model_fields.get("degree_type") or "").strip().lower()
    if dt not in {"bachelor", "master", "dissertation"}:
        dt = degree if degree != "unknown" else "unknown"

    model_phrase = str(model_fields.get("degree_phrase") or "").strip()
    if not (model_phrase and verbatim_ok(model_phrase, hay)):
        model_phrase = ""

    uni_guess = str(model_fields.get("university") or "").strip()
    uni, ur, uni_hits = resolve_university(
        cover_text, uni_guess, universities, templates_dir
    )
    risks.extend(ur)

    tpl_guess = str(model_fields.get("template") or "").strip()
    tpl_names = available_names
    template_learned = False
    notes: list[str] = []
    hard = hardcoded_template_path(uni, templates_dir)
    if hard is not None:
        template_name = hard.name
        uni = {**uni, "template": template_name}
    else:
        tpl_path, tr, tpl_names = resolve_template(
            cover_text, tpl_guess, templates_dir, universities
        )
        risks.extend(tr)
        if tpl_path is None and "no_template_on_cover" in tr:
            # No filename on the cover matched: ask the model to search the
            # whole template library (fuzzy: city, faculty, abbreviations).
            tpl_path, mr = model_search_templates(
                cover_text, meta, cfg, uni_guess, author,
                str(model_fields.get("degree_type") or ""),
            )
            if mr:
                notes.append(mr)
                if tpl_path is not None:
                    risks.append("template_found_by_model_search")
        if tpl_path is not None:
            template_name = tpl_path.name
            template_learned = True
            risks.append("template_learned")
            if uni is None:
                toks = distinctive_filename_tokens(tpl_path.name)
                uni = {
                    "id": safe_stem(toks[0] if toks else tpl_path.stem),
                    "aliases": toks,
                    "template": template_name,
                }
            else:
                uni = {**uni, "template": template_name}
        else:
            template_name = ""

    labels = cfg.get("degree_labels", {})
    printed = model_phrase or exact_degree_phrase(cover_text)
    if printed:
        degree_text = printed
    else:
        degree_text = labels.get(dt, labels.get("unknown", "Thesis"))
        risks.append("degree_inferred_not_on_cover")

    blocking = any(
        r == "multiple_years_unresolved"
        or r == "author_looks_like_supervisor"
        or r.startswith("ambiguous_template:")
        or r == "no_template_on_cover"
        or (r == "no_university_on_cover" and not template_name)
        or (r.startswith("ambiguous_university:") and not template_name)
        for r in risks
    )
    needs_review = any(
        r in {
            "university_disambiguated_by_model",
            "university_from_template_filename",
            "author_looks_like_supervisor",
        }
        for r in risks
    )
    ok_to_stamp = (not blocking) and (not needs_review) and bool(uni) and bool(author) and bool(year)

    return {
        "university_id": uni["id"] if uni else "",
        "university_template": template_name or (uni["template"] if uni else ""),
        "university_raw": uni_guess,
        "university_matches": uni_hits,
        "template_candidates": tpl_names,
        "template_model": tpl_guess,
        "template_learned": template_learned,
        "author": author,
        "title": title,
        "degree_type": dt,
        "degree_text": degree_text,
        "year": year,
        "candidates": candidates,
        "risks": risks,
        "notes": notes,
        "ok_to_stamp": ok_to_stamp,
        "model_raw": model_fields,
    }


# ---------------------------------------------------------------------------
# UPF text blocks
# ---------------------------------------------------------------------------

@dataclass
class GlyphLine:
    align: str
    glyphs: list[tuple[str, str, str, str]]  # char, font, size, weight
    plain: str | None = None  # List<string> stores whole line here

    def text(self) -> str:
        if self.plain is not None:
            return self.plain
        return "".join(g[0] for g in self.glyphs)


@dataclass
class TextBlock:
    start: int
    end: int
    raw: str
    kind: str  # "glyph" | "string" | "unknown"
    lines: list[GlyphLine] = field(default_factory=list)

    def decoded_lines(self) -> list[str]:
        return [ln.text() for ln in self.lines]

    def joined(self) -> str:
        return " ".join(t for t in self.decoded_lines() if t.strip())

    def is_empty(self) -> bool:
        cleaned = re.sub(r"[\x00-\x1f]", "", self.joined()).strip()
        return not cleaned

    def font_defaults(self) -> tuple[str, str, str, str]:
        for ln in self.lines:
            for ch, font, size, weight in ln.glyphs:
                if ch.strip():
                    return ln.align, font, size, weight
        return "Center", "Cambria", "20", "Regular"

    def line_font(self, index: int) -> tuple[str, str, str, str]:
        if 0 <= index < len(self.lines):
            ln = self.lines[index]
            for ch, font, size, weight in ln.glyphs:
                if ch.strip():
                    return ln.align, font, size, weight
            if ln.align:
                align, font, size, weight = self.font_defaults()
                return ln.align, font, size, weight
        return self.font_defaults()


def _matching_brace(s: str, open_idx: int) -> int:
    depth = 0
    for i in range(open_idx, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def find_text_blocks(upf: str) -> list[TextBlock]:
    blocks: list[TextBlock] = []
    for m in re.finditer(r"Object:TextDesignElement\s*\{", upf):
        brace_open = upf.find("{", m.end() - 1)
        brace_close = _matching_brace(upf, brace_open)
        if brace_close < 0:
            continue
        raw = upf[m.start() : brace_close + 1]
        if "List<TextLineDetails>" in raw:
            kind, lines = "glyph", parse_glyph_lines(raw)
        elif "List<string>" in raw:
            kind, lines = "string", parse_string_lines(raw)
        else:
            kind, lines = "unknown", []
        blocks.append(
            TextBlock(start=m.start(), end=brace_close + 1, raw=raw, kind=kind, lines=lines)
        )
    return blocks


def _detab_lead(line: str) -> str:
    return re.sub(r"^\t+", "", line).rstrip("\t")


def parse_glyph_lines(block: str) -> list[GlyphLine]:
    m = re.search(r"List<TextLineDetails>:(\d+)\s*\{", block)
    if not m:
        return []
    list_open = block.find("{", m.end() - 1)
    list_close = _matching_brace(block, list_open)
    body = block[list_open + 1 : list_close]
    lines: list[GlyphLine] = []
    for lm in re.finditer(r"(\d+)\s*\{", body):
        inner_open = body.find("{", lm.end() - 1)
        inner_close = _matching_brace(body, inner_open)
        if inner_close < 0:
            continue
        inner = body[inner_open + 1 : inner_close]
        align = "Center"
        glyphs: list[tuple[str, str, str, str]] = []
        saw_align = False
        for row in inner.splitlines():
            if not row.strip():
                continue
            detab = _detab_lead(row)
            if not saw_align and detab in {"Center", "Left", "Right", "Justify"}:
                align = detab
                saw_align = True
                continue
            parts = detab.split(";")
            if len(parts) < 4:
                continue
            ch, font, size, weight = parts[0], parts[1], parts[2], parts[3]
            # Prägen stores a space as a one-character first field; never strip it.
            if ch == "" and row.lstrip("\t")[:1] == " ":
                ch = " "
            glyphs.append((ch, font, size, weight))
        lines.append(GlyphLine(align=align, glyphs=glyphs))
    return lines


def parse_string_lines(block: str) -> list[GlyphLine]:
    m = re.search(r"List<string>:(\d+)\s*\{", block)
    if not m:
        return []
    list_open = block.find("{", m.end() - 1)
    list_close = _matching_brace(block, list_open)
    body = block[list_open + 1 : list_close]
    lines: list[GlyphLine] = []
    for lm in re.finditer(r"(\d+)\s*\{", body):
        inner_open = body.find("{", lm.end() - 1)
        inner_close = _matching_brace(body, inner_open)
        if inner_close < 0:
            continue
        inner = body[inner_open + 1 : inner_close]
        plain = ""
        for raw_line in inner.splitlines():
            if raw_line.strip("\t") == "" and plain == "":
                continue
            plain = _detab_lead(raw_line)
            break
        lines.append(GlyphLine(align="Center", glyphs=[], plain=plain))
    return lines


def _list_indent(block: TextBlock) -> str:
    m = re.search(r"List<(?:TextLineDetails|string)>:", block.raw)
    if not m:
        return "\t\t\t\t\t\t"
    line_start = block.raw.rfind("\n", 0, m.start()) + 1
    return block.raw[line_start : m.start()]


def encode_glyph_line(
    text: str, align: str, font: str, size: str, weight: str, indent: str
) -> str:
    # Native Prägen files keep Center + glyph rows unindented; only counts/braces are tabbed.
    rows = [align] + [f"{ch};{font};{size};{weight}" for ch in text]
    inner = "\n".join(rows)
    return f"{indent}{len(text)}\n{indent}{{\n{inner}\n{indent}}}"


def encode_string_line(text: str, indent: str) -> str:
    return f"{indent}{len(text)}\n{indent}{{\n{text}\n{indent}}}"


def replace_list(block: TextBlock, new_lines: list[str]) -> str:
    indent = _list_indent(block)
    inner_indent = indent + "\t"
    if block.kind == "glyph":
        tag = r"List<TextLineDetails>:(\d+)\s*\{"
        pieces = []
        for i, t in enumerate(new_lines):
            align, font, size, weight = block.line_font(i)
            pieces.append(encode_glyph_line(t, align, font, size, weight, inner_indent))
        encoded = "\n".join(pieces)
        header = f"List<TextLineDetails>:{len(new_lines)}"
    elif block.kind == "string":
        tag = r"List<string>:(\d+)\s*\{"
        encoded = "\n".join(encode_string_line(t, inner_indent) for t in new_lines)
        header = f"List<string>:{len(new_lines)}"
    else:
        raise ValueError("TextDesignElement has no supported text list")
    m = re.search(tag, block.raw)
    if not m:
        raise ValueError("Could not find text list inside TextDesignElement")
    list_open = block.raw.find("{", m.end() - 1)
    list_close = _matching_brace(block.raw, list_open)
    new_list = f"{header}\n{indent}{{\n{encoded}\n{indent}}}"
    return block.raw[: m.start()] + new_list + block.raw[list_close + 1 :]


def classify_blocks(blocks: list[TextBlock], placeholders: dict) -> dict[str, int]:
    """Map role -> original block index. Ignores empty / junk fields."""
    roles: dict[str, int] = {}
    deg_ph = [norm(x) for x in placeholders.get("degree", [])] + [norm(p) for p in DEGREE_PHRASES]
    auth_ph = [norm(x) for x in placeholders.get("author", [])]
    year_ph = [norm(x) for x in placeholders.get("year", [])]
    title_ph = [norm(x) for x in placeholders.get("title", [])]

    usable = [(i, b) for i, b in enumerate(blocks) if not b.is_empty()]

    for i, b in usable:
        joined = norm(b.joined())
        texts = [norm(t) for t in b.decoded_lines()]
        if any(p and p in joined for p in deg_ph) and "degree" not in roles:
            roles["degree"] = i
        if any(re.fullmatch(r"(19|20)\d{2}", t.strip()) or t in year_ph for t in texts):
            roles.setdefault("author_year", i)
        if any(p and p in joined for p in auth_ph):
            roles.setdefault("author_year", i)
        if any(p and p in joined for p in title_ph) and "title" not in roles:
            roles["title"] = i

    usable_idx = [i for i, _ in usable]
    n = len(usable_idx)
    if "degree" not in roles and n >= 1:
        roles["degree"] = usable_idx[0]
    if n == 2:
        roles.setdefault("author_year", usable_idx[1])
        roles.pop("title", None)
    elif n >= 3:
        roles.setdefault("title", usable_idx[1])
        roles.setdefault("author_year", usable_idx[2])
    return roles


def fill_upf(template_text: str, fields: dict, cfg: dict) -> str:
    blocks = find_text_blocks(template_text)
    if not blocks:
        raise ValueError("No Object:TextDesignElement found in UPF")

    fillable = [b for b in blocks if not b.is_empty()]
    roles = classify_blocks(blocks, cfg.get("placeholders", {}))
    LOG.info(
        "UPF text fields: total=%s fillable=%s roles=%s decoded=%s",
        len(blocks),
        len(fillable),
        roles,
        [b.decoded_lines() for b in fillable],
    )
    if "author_year" not in roles:
        raise ValueError(
            "This UPF has no author/year field (only school branding). "
            "Use a cover that already contains a name + year dummy."
        )

    updates: dict[int, list[str]] = {}

    if "degree" in roles:
        updates[roles["degree"]] = [fields["degree_text"]]

    if "title" in roles and fields.get("title"):
        updates[roles["title"]] = [fields["title"]]

    if "author_year" in roles:
        idx = roles["author_year"]
        old = blocks[idx].decoded_lines()
        new_lines = list(old) if old else ["", ""]
        author_i, year_i = 0, None
        for i, t in enumerate(old):
            if re.fullmatch(r"(19|20)\d{2}", t.strip()) or norm(t) in {
                norm(x) for x in cfg.get("placeholders", {}).get("year", [])
            }:
                year_i = i
                break
        if year_i is None:
            year_i = 1 if len(new_lines) > 1 else None
            if year_i is None:
                new_lines.append("")
                year_i = 1
        while len(new_lines) <= max(author_i, year_i):
            new_lines.append("")
        lead = ""
        if old and year_i < len(old) and old[year_i].startswith(" ") and not str(fields["year"]).startswith(" "):
            lead = " "
        new_lines[author_i] = fields["author"]
        new_lines[year_i] = lead + str(fields["year"])
        updates[idx] = new_lines

    out = template_text
    for idx in sorted(updates, reverse=True):
        block = blocks[idx]
        new_raw = replace_list(block, updates[idx])
        out = out[: block.start] + new_raw + out[block.end :]
    return out


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------

def safe_stem(name: str) -> str:
    name = re.sub(r"[^\w\-]+", "_", name, flags=re.U)
    return name.strip("_") or "cover"


TITLE_NAME_RE = re.compile(r"(?i)(titel|title)")


def is_title_template(name: str) -> bool:
    return TITLE_NAME_RE.search(Path(name).stem) is not None


def template_family(name: str) -> str:
    stem = Path(name).stem
    stem = re.sub(r"^\d+-", "", stem)
    stem = TITLE_NAME_RE.sub("", stem)
    stem = re.sub(r"[_-]+", " ", stem)
    return norm(stem)


def find_template_pair(chosen: str, templates_dir: Path) -> tuple[Path | None, Path | None]:
    """Regular cover + TITEL/TITLE sibling that share a filename family."""
    untitled: Path | None = None
    titled: Path | None = None
    family = template_family(chosen)
    for path in templates_dir.glob("*.upf"):
        if template_family(path.name) != family:
            continue
        if is_title_template(path.name):
            titled = path
        else:
            untitled = path
    chosen_path = templates_dir / chosen
    if chosen_path.is_file():
        if is_title_template(chosen):
            titled = chosen_path
        else:
            untitled = chosen_path
    return untitled, titled


def template_variants_for(
    uni: dict | None, chosen: str | None, templates_dir: Path
) -> list[Path]:
    """Every plain .upf template that belongs to the same university.

    Membership (either rule qualifies):
    (a) every distinctive token of the template filename appears in the
        university's aliases/template name — so "Wuppertal Groß-Logo" and
        "Wuppertal Rund" both match a uni aliased "wuppertal", while
        "Berufskolleg Barmen" does not match a uni aliased only "barmen";
    (b) it shares the chosen template's filename family (Muster / Muster-2 …).
    The chosen template is always first when it exists.
    """
    out: list[Path] = []
    seen: set[str] = set()

    def add(p: Path | None) -> None:
        if p is None or not p.is_file() or is_title_template(p.name):
            return
        key = str(p.resolve())
        if key not in seen:
            seen.add(key)
            out.append(p)

    add(templates_dir / chosen if chosen else None)

    alias_blob = norm(" ".join(_uni_aliases(uni))) if uni else ""
    family = template_family(chosen) if chosen else None
    for path in list_plain_templates(templates_dir):
        toks = [norm(t) for t in distinctive_filename_tokens(path.name)]
        if toks and alias_blob and all(t in alias_blob for t in toks):
            # Require at least one long identifying token, so a template whose
            # ONLY distinctive token is the city ("Universität_zu_köln") is
            # not confused with another school in the same city (TH Köln).
            if any(len(t) >= 6 for t in toks):
                add(path)
        elif family is not None and template_family(path.name) == family:
            add(path)
    return out


FAIL_SYSTEM = """You write a short failure report for an operator who foil-stamps thesis covers.
Plain text only, no markdown, 6–12 lines.
Match the language of COVER_TEXT (German or English).
Explain why no cover was produced (or why a variant is missing), what was found on pages 1–2, and what a human should check.
Do not invent an author, university, title or year that is not in COVER_TEXT.
"""


def _fallback_fail_note(error: str, fields: dict) -> str:
    bits = [
        f"Kein Cover geschrieben: {error}",
        f"Universität: {fields.get('university_id') or fields.get('university_raw') or '—'}",
        f"Autor: {fields.get('author') or '—'}",
        f"Jahr: {fields.get('year') or '—'}",
        f"Titel: {fields.get('title') or '—'}",
    ]
    if fields.get("risks"):
        bits.append("Risiken: " + ", ".join(fields["risks"]))
    bits.append("Bitte Deckblatt (Seiten 1–2) und Aliase in config.json prüfen.")
    return "\n".join(bits)


def _local_fail_dir() -> Path:
    """Always-writable fallback location next to the script."""
    return Path(__file__).resolve().parent / "failed_reports"


def write_fail_report(
    pdf_path: Path,
    cfg: dict,
    error: BaseException,
    cover_text: str = "",
    fields: dict | None = None,
    source: str = "",
) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    # Local dir is always in the list: if the network share is down (the
    # most common reason the whole pipeline fails), the report still lands.
    folders = []
    for key in ("output_dir", "failed_dir"):
        val = cfg.get(key)
        if val:
            p = Path(val)
            if p not in folders:
                folders.append(p)
    folders.append(_local_fail_dir())
    facts = {
        "pdf": str(pdf_path),
        "error": f"{type(error).__name__}: {error}",
        "text_source": source,
        "fields": {
            k: fields.get(k)
            for k in (
                "university_id",
                "university_template",
                "university_raw",
                "university_matches",
                "author",
                "title",
                "degree_text",
                "year",
                "risks",
                "ok_to_stamp",
                "model_raw",
            )
            if fields
        },
    }
    model_note = ""
    mcfg = cfg.get("model", {})
    if cover_text and mcfg.get("backend", "ollama") != "none":
        user = (
            f"ERROR: {facts['error']}\n"
            f"EXTRACTED: {json.dumps(facts['fields'], ensure_ascii=False)}\n"
            f"COVER_TEXT:\n{cover_text_for_model(cover_text, 2000)}\n"
        )
        try:
            if mcfg.get("backend", "ollama") == "ollama":
                model_note = call_ollama(
                    mcfg.get("name", "qwen3:8b"),
                    FAIL_SYSTEM,
                    user,
                    mcfg.get("host", "http://[IP_ADDRESS]:11434"),
                    min(int(mcfg.get("timeout", 180)), 45),
                    json_mode=False,
                    num_predict=400,
                    think=model_think_value(mcfg),
                    num_ctx=int(mcfg.get("num_ctx", 8192)),
                    keep_alive=str(mcfg.get("keep_alive", "30m")),
                )
            else:
                model_note = call_openai(
                    mcfg.get("name", "qwen3:8b"),
                    FAIL_SYSTEM,
                    user,
                    mcfg.get("openai_base", "http://[IP_ADDRESS]:1234/v1"),
                    mcfg.get("api_key", "not-needed"),
                    min(int(mcfg.get("timeout", 180)), 45),
                    json_mode=False,
                    num_predict=400,
                )
        except Exception as exc:
            model_note = _fallback_fail_note(facts["error"], facts["fields"]) + f"\n(model report failed: {exc})"
    if not (model_note or "").strip():
        model_note = _fallback_fail_note(facts["error"], facts["fields"])
    body = [
        f"FAIL  {pdf_path.name}",
        f"time  {stamp}",
        f"error {facts['error']}",
        "",
        "--- extracted ---",
        json.dumps(facts["fields"], ensure_ascii=False, indent=2),
        "",
        "--- model report ---",
        (model_note or "(no model report)").strip(),
        "",
        "--- cover text (truncated) ---",
        cover_text[:2000],
    ]
    text_out = "\n".join(body)
    dest: Path | None = None
    for folder in folders:
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except Exception:
            LOG.exception("Could not create %s for failure report", folder)
            continue
        dest = folder / f"{safe_stem(pdf_path.stem)}_{stamp}_FAIL.txt"
        try:
            retry_io(lambda d=dest: d.write_text(text_out, encoding="utf-8"), what=f"writing {dest.name}")
            LOG.error("Wrote failure report %s", dest)
        except Exception:
            # One unreachable folder must not stop the others.
            LOG.exception("Could not write failure report to %s", folder)
    if dest is None:
        dest = _local_fail_dir() / f"{safe_stem(pdf_path.stem)}_{stamp}_FAIL.txt"
        dest.write_text(text_out, encoding="utf-8")
        LOG.error("Wrote failure report %s", dest)
    return dest


_TEMPLATE_TEXT: dict[tuple[str, float], str] = {}


def _read_template(path: Path) -> str:
    key = (str(path.resolve()), path.stat().st_mtime)
    cached = _TEMPLATE_TEXT.get(key)
    if cached is not None:
        return cached
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(_TEMPLATE_TEXT) > 32:
        _TEMPLATE_TEXT.clear()
    _TEMPLATE_TEXT[key] = text
    return text


def output_base_name(fields: dict) -> str:
    """author_year_university — pair is then `{base}.upf` and `{base}_TITLE.upf`."""
    parts = [
        safe_stem(fields.get("author") or "author"),
        str(fields.get("year") or "").strip() or "year",
        safe_stem(fields.get("university_id") or "uni"),
    ]
    return "_".join(parts)


def allocate_output_paths(out_dir: Path, base: str) -> tuple[Path, Path, Path]:
    """Unused `{base}.upf` / `{base}_TITLE.upf` / `{base}.json`, with _1, _2, … on clash."""
    n = 0
    while True:
        extra = f"_{n}" if n else ""
        stem = f"{base}{extra}"
        plain = out_dir / f"{stem}.upf"
        titled = out_dir / f"{stem}_TITLE.upf"
        sidecar = out_dir / f"{stem}.json"
        if not any(p.exists() for p in (plain, titled, sidecar)):
            return plain, titled, sidecar
        n += 1


def _write_upf(template_path: Path, fields: dict, cfg: dict, out_path: Path) -> Path:
    filled = fill_upf(_read_template(template_path), fields, cfg)
    retry_io(lambda: out_path.write_text(filled, encoding="utf-8"), what=f"writing {out_path.name}")
    LOG.info("Wrote %s from %s", out_path.name, template_path.name)
    return out_path


def process_pdf(pdf_path: Path, cfg: dict) -> list[Path]:
    LOG.info("Processing %s", pdf_path)
    ctx: dict = {"text": "", "source": "", "fields": {}}
    try:
        return _process_pdf_inner(pdf_path, cfg, ctx)
    except Exception as exc:
        try:
            write_fail_report(
                pdf_path,
                cfg,
                exc,
                ctx.get("text") or "",
                ctx.get("fields") or {},
                ctx.get("source") or "",
            )
        except Exception:
            # Belt and braces: even the report writer failed. A minimal local
            # report guarantees the operator always gets an error text.
            LOG.exception("Standard failure report failed for %s", pdf_path)
            try:
                dest = _local_fail_dir() / (
                    f"{safe_stem(pdf_path.stem)}_{datetime.now().strftime('%Y%m%d-%H%M%S')}_FAIL.txt"
                )
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(
                    f"FAIL  {pdf_path.name}\n"
                    f"time  {datetime.now().isoformat()}\n"
                    f"error {type(exc).__name__}: {exc}\n"
                    f"(full report generation also failed — see service log)\n",
                    encoding="utf-8",
                )
                LOG.error("Wrote minimal failure report %s", dest)
            except Exception:
                LOG.exception("Could not write any failure report for %s", pdf_path)
        raise


def _process_pdf_inner(pdf_path: Path, cfg: dict, ctx: dict) -> list[Path]:
    text, source = get_cover_text(pdf_path, cfg)
    ctx["text"], ctx["source"] = text, source
    LOG.info("Cover text source=%s chars=%s", source, len(text))
    if not text.strip():
        raise RuntimeError("No text from first two pages (digital + OCR empty)")

    meta = pdf_metadata(pdf_path)
    fields = extract_fields(text, meta, cfg)
    ctx["fields"] = fields
    LOG.info("Resolved fields: %s", {k: v for k, v in fields.items() if k not in {"candidates", "model_raw"}})
    if fields.get("risks"):
        LOG.warning("Risks: %s  ok_to_stamp=%s", fields["risks"], fields.get("ok_to_stamp"))

    amb = [r for r in fields.get("risks", []) if r.startswith("ambiguous_university:")]
    if amb:
        raise RuntimeError(
            f"Cover names more than one configured university ({amb[0]}). "
            "Model did not uniquely pick one of those cover matches. Refusing to stamp."
        )
    if "no_university_on_cover" in fields.get("risks", []):
        raise RuntimeError(
            f"No university alias on pages 1–2. Model said {fields['university_raw']!r} "
            "(ignored because it is not on the cover). Add aliases or put the school name on the PDF."
        )
    if "author_looks_like_supervisor" in fields.get("risks", []):
        raise RuntimeError(
            f"Author looks like a supervisor, not the candidate: {fields['author']!r}. Refusing to stamp."
        )
    if "multiple_years_unresolved" in fields.get("risks", []):
        raise RuntimeError(
            f"Several years on the cover {fields.get('candidates', {}).get('years')}; "
            "model did not pick a publishing year that appears on the cover. Refusing to stamp."
        )

    if not fields["author"] or not fields["year"]:
        raise RuntimeError(f"Missing author or year: author={fields['author']!r} year={fields['year']!r}")

    templates_dir = Path(cfg["templates_dir"])
    uni_obj = next(
        (u for u in cfg.get("universities", []) if u.get("id") == fields["university_id"]),
        None,
    )
    variants = template_variants_for(uni_obj, fields["university_template"], templates_dir)
    if not variants:
        if not fields["university_template"]:
            raise RuntimeError(
                f"Could not match a .upf template. Model said template={fields.get('template_model')!r} "
                f"university={fields['university_raw']!r}. "
                f"Cover-grounded files were: {fields.get('template_candidates') or []}."
            )
        raise RuntimeError(f"Template not found: {templates_dir / fields['university_template']}")
    if not fields["university_template"]:
        fields["university_template"] = variants[0].name
    if len(variants) > 1:
        note = (
            f"{len(variants)} matching templates found for university "
            f"{fields['university_id']!r} — one stamped cover written per template: "
            + ", ".join(p.name for p in variants)
        )
        fields["notes"].append(note)
        LOG.info("%s", note)

    out_dir = Path(cfg["output_dir"])
    ensure_dirs(out_dir)
    _, _, sidecar = allocate_output_paths(out_dir, output_base_name(fields))
    written: list[Path] = []
    outputs: list[dict] = []
    variant_errors: list[str] = []

    # Optional extra file per template: the TITEL/TITLE sibling variant.
    # Off by default; enable with "write_title_variant": true.
    write_title = bool(cfg.get("write_title_variant", False))
    single = len(variants) == 1

    for tpl in variants:
        base = output_base_name(fields)
        if not single:
            # Multiple variants: make each output file identifiable by its
            # source template, e.g. `…_th-koeln_Koln_TH_Muster.upf`.
            base = f"{base}_{safe_stem(tpl.stem)}"
        plain_path, _tp, _sc = allocate_output_paths(out_dir, base)
        try:
            path = _write_upf(tpl, fields, cfg, plain_path)
            written.append(path)
            outputs.append({"variant": "plain", "template": tpl.name, "path": str(path)})
        except Exception as exc:
            variant_errors.append(f"plain ({tpl.name}): {exc}")
            LOG.exception("Failed to fill template %s", tpl)

        if write_title:
            _u, titled = find_template_pair(tpl.name, templates_dir)
            if titled is not None:
                title_base = output_base_name(fields)
                if not single:
                    title_base = f"{title_base}_{safe_stem(titled.stem)}"
                title_path, _t2, _s2 = allocate_output_paths(out_dir, title_base)
                try:
                    path = _write_upf(titled, fields, cfg, title_path)
                    written.append(path)
                    outputs.append({"variant": "title", "template": titled.name, "path": str(path)})
                except Exception as exc:
                    variant_errors.append(f"title ({titled.name}): {exc}")
                    LOG.exception("Failed to fill title template %s", titled)
            else:
                variant_errors.append(f"no TITEL/TITLE sibling next to {tpl.name}")
                LOG.info("No TITEL/TITLE sibling for family of %s", tpl.name)

    if not written:
        raise RuntimeError("Could not write any UPF: " + "; ".join(variant_errors))

    tpl_name = fields.get("university_template") or ""
    uni_id = fields.get("university_id") or ""
    already = any(
        u.get("id") == uni_id and u.get("template") == tpl_name
        for u in cfg.get("universities", [])
    )
    if tpl_name and not already:
        extra = distinctive_filename_tokens(tpl_name)
        raw_uni = str(fields.get("university_raw") or "").strip()
        if raw_uni:
            extra.append(raw_uni)
        persist_learned_template(cfg, uni_id or safe_stem(tpl_name), tpl_name, extra)
        fields["template_learned"] = True
        if "template_learned" not in fields.get("risks", []):
            fields.setdefault("risks", []).append("template_learned")

    retry_io(
        lambda: sidecar.write_text(
            json.dumps(
                {
                    "pdf": str(pdf_path),
                    "text_source": source,
                    "ok_to_stamp": fields.get("ok_to_stamp", True),
                    "risks": fields.get("risks", []),
                    "notes": fields.get("notes", []),
                    "university_matches": fields.get("university_matches", []),
                    "outputs": outputs,
                    "variant_errors": variant_errors,
                    "fields": fields,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        ),
        what=f"writing {sidecar.name}",
    )
    if fields.get("notes"):
        notes_path = sidecar.with_name(sidecar.stem + "_NOTES.txt")
        notes_path.write_text("\n\n".join(fields["notes"]).strip() + "\n", encoding="utf-8")
        LOG.info("Wrote model notes %s", notes_path.name)
    if variant_errors:
        LOG.warning("Variant issues: %s", variant_errors)
    if not fields.get("ok_to_stamp", True):
        LOG.warning("Wrote %s but ok_to_stamp=false — review sidecar before production", written)
    return written


def move_aside(pdf_path: Path, dest_dir: Path) -> None:
    ensure_dirs(dest_dir)
    target = dest_dir / pdf_path.name
    if target.exists():
        target = dest_dir / f"{pdf_path.stem}_{int(time.time())}{pdf_path.suffix}"
    retry_io(lambda: shutil.move(str(pdf_path), str(target)), what=f"moving {pdf_path.name}")


class PdfHandler(FileSystemEventHandler):
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._seen: set[str] = set()

    def on_created(self, event):  # noqa: N802
        if getattr(event, "is_directory", False):
            return
        self._maybe(Path(event.src_path))

    def on_moved(self, event):  # noqa: N802
        if getattr(event, "is_directory", False):
            return
        self._maybe(Path(event.dest_path))

    def _maybe(self, path: Path) -> None:
        if path.suffix.lower() != ".pdf":
            return
        key = str(path.resolve()) if path.exists() else str(path)
        if key in self._seen:
            return
        self._seen.add(key)
        if len(self._seen) > 4000:
            self._seen = set(list(self._seen)[-2000:])
        settle = float(self.cfg.get("watch_settle_seconds", 1.5))
        if not wait_until_stable(path, settle):
            LOG.warning("File never stabilized: %s", path)
            return
        try:
            process_pdf(path, self.cfg)
            if self.cfg.get("processed_dir"):
                move_aside(path, Path(self.cfg["processed_dir"]))
        except Exception as exc:
            LOG.error("Failed on %s: %s", path, exc)
            if self.cfg.get("failed_dir"):
                move_aside(path, Path(self.cfg["failed_dir"]))


def scan_existing(cfg: dict) -> None:
    watch = Path(cfg["watch_dir"])
    if not watch.is_dir():
        LOG.error("watch_dir not found: %s", watch)
        return
    pdfs = sorted(watch.glob("*.pdf")) + sorted(watch.glob("*.PDF"))
    seen: set[str] = set()
    unique: list[Path] = []
    for pdf in pdfs:
        key = str(pdf.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(pdf)
    if not unique:
        LOG.info("No PDFs in %s", watch)
    for pdf in unique:
        try:
            process_pdf(pdf, cfg)
            if cfg.get("processed_dir"):
                move_aside(pdf, Path(cfg["processed_dir"]))
        except Exception as exc:
            LOG.error("Failed on existing %s: %s", pdf, exc)
            if cfg.get("failed_dir"):
                move_aside(pdf, Path(cfg["failed_dir"]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=Path("config.json"))
    ap.add_argument("--once", type=Path, help="Process a single PDF and exit")
    ap.add_argument("--scan-existing", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    for noisy in ("pdfminer", "pdfminer.pdfinterp", "pdfminer.pdfpage", "pdfminer.psparser", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if not args.config.is_file():
        LOG.error("Config not found: %s", args.config)
        return 2
    cfg = load_config(args.config)
    ensure_dirs(
        Path(cfg["watch_dir"]),
        Path(cfg["output_dir"]),
        Path(cfg["templates_dir"]),
        Path(cfg.get("processed_dir") or str(Path(cfg["watch_dir"]) / "processed")),
        Path(cfg.get("failed_dir") or str(Path(cfg["watch_dir"]) / "failed")),
    )

    if args.once:
        if not args.once.is_file():
            LOG.error("PDF not found: %s", args.once)
            return 2
        try:
            process_pdf(args.once, cfg)
            return 0
        except Exception as exc:
            LOG.error("Failed on %s: %s", args.once, exc)
            return 1

    if args.scan_existing:
        scan_existing(cfg)

    if Observer is None:
        LOG.error("watchdog is not installed. pip install watchdog  (or use --once)")
        return 1

    handler = PdfHandler(cfg)
    use_polling = bool(cfg.get("watch_polling", True))
    observer = None
    if use_polling and PollingObserver is not None:
        observer = PollingObserver(timeout=float(cfg.get("watch_poll_interval", 5.0)))
        LOG.info("Using polling observer (interval=%ss) for network share", cfg.get("watch_poll_interval", 5.0))
    elif Observer is not None:
        observer = Observer()
        if use_polling:
            LOG.warning("PollingObserver unavailable — falling back to inotify Observer (may miss events on network shares)")
    else:
        LOG.error("watchdog is not installed. pip install watchdog  (or use --once)")
        return 1
    observer.schedule(handler, str(Path(cfg["watch_dir"])), recursive=False)
    observer.start()
    LOG.info("Watching %s  →  %s", cfg["watch_dir"], cfg["output_dir"])
    try:
        if cfg.get("watch_rescan_interval"):
            interval = float(cfg["watch_rescan_interval"])
            last = time.time()
            while True:
                time.sleep(1)
                if time.time() - last >= interval:
                    last = time.time()
                    scan_existing(cfg)
        else:
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
