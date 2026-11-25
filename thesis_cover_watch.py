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
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.request
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from cover_model import (
    InvalidModelResponse, ModelError, ModelOutcome, ModelUnavailable,
    capture_inference, inference_metrics, request_json, require_content,
)
from cover_jobs import JobState, RetryableJobError, atomic_write, job_lock, staged_pdf
from cover_extract import (
    CoverEvidence, compact_cover, extract_digital, extract_ocr,
    extract_page_image_text, get_cover_text, pdf_metadata,
)
from cover_config import normalize_config
from cover_queue import PdfQueue
from cover_fields import can_stamp, set_institution_notes
from cover_upf import (
    GlyphLine, TextBlock, _matching_brace, encode_glyph_line, encode_string_line,
    find_text_blocks, parse_glyph_lines, parse_string_lines, replace_list,
)

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
    data = normalize_config(json.loads(path.read_text(encoding="utf-8")), path.resolve().parent)
    data["_config_path"] = str(path.resolve())
    return data


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
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            stat = path.stat()
            size = (stat.st_size, stat.st_mtime_ns)
        except FileNotFoundError:
            return False
        if size == last and size[0] > 0:
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= settle:
                return True
        else:
            stable_since = None
            last = size
        time.sleep(0.2)
    try:
        # The deadline passed while the file kept changing. Accept it only
        # if the writes have actually stopped; otherwise a slow copy is
        # still in flight and the periodic rescan will retry it later.
        stat = path.stat()
        return (stable_since is not None
                and time.monotonic() - stable_since >= settle
                and (stat.st_size, stat.st_mtime_ns) == last)
    except FileNotFoundError:
        return False


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
    (r"\b(master'?s?\s+thesis|masterarbeit|master-thesis|masterthesis|"
     r"master\s+of\s+(arts|science|engineering|laws|education|business)|m\.?\s*sc\.?|m\.?\s*a\.?|magister)\b", "master"),
    (r"\b(bachelor'?s?\s+thesis|bachelorarbeit|bachelor-thesis|bachelorthesis|projektarbeit|"
     r"bachelor\s+of\s+(arts|science|engineering|laws|education|business)|b\.?\s*sc\.?|b\.?\s*a\.?)\b", "bachelor"),
]

# Closed whitelist of degree wordings that may ever be stamped. Covers
# also print degree-programme names like "Bachelor of Arts" which must
# never leak into the stamp, so anything outside this list falls back to
# the configured degree_labels. This list is also the model's menu: the
# SYSTEM prompt is generated from it (see _DEGREE_OPTIONS below).
DEGREE_ALLOWED = [
    # Bachelor
    "Bachelor-Thesis",
    "Bachelorthesis",
    "Bachelorarbeit",
    "Bachelor Thesis",
    "Bachelor's Thesis",
    # Master
    "Master-Thesis",
    "Masterthesis",
    "Masterarbeit",
    "Master Thesis",
    "Master's Thesis",
    # Diplom
    "Diplomarbeit",
    "Diplom-Thesis",
    "Diplomthesis",
    "Diploma Thesis",
    # Projekt
    "Projektarbeit",
    # Dissertation / Promotion
    "Dissertation",
    "Doktorarbeit",
    "Doctoral Thesis",
    "Promotionsarbeit",
    "PhD Thesis",
    "Ph.D. Thesis",
    # Generic "final thesis" wordings — name the kind without naming the
    # degree, and some covers use only these (singular and plural).
    "Wissenschaftliche Abschlussarbeit",
    "Wissenschaftliche Abschlussarbeiten",
    "Wissenschaftliche Arbeit",
    "Wissenschaftliche Arbeiten",
    "Wissenschaftliche Hausarbeit",
    "Wissenschaftliche Hausarbeiten",
    "Abschlussarbeit",
    "Abschlussarbeiten",
    "Studienarbeit",
    "Studienarbeiten",
    "Seminararbeit",
    "Seminararbeiten",
    "Hausarbeit",
    "Hausarbeiten",
    "Examensarbeit",
    "Examensarbeiten",
    "Zulassungsarbeit",
    "Zulassungsarbeiten",
    "Thesis",
]

# Prompt menu, derived so a new entry never has to be duplicated by hand.
_DEGREE_OPTIONS = ", ".join(DEGREE_ALLOWED)

# Degree-agnostic final-thesis wordings: they name the kind without naming
# the degree. When the degree type is known (bachelor/master/dissertation),
# the configured degree label is stamped instead of the vague word.
DEGREE_AGNOSTIC = {
    norm(w)
    for w in (
        "Thesis",
        "Abschlussarbeit",
        "Abschlussarbeiten",
        "Wissenschaftliche Abschlussarbeit",
        "Wissenschaftliche Abschlussarbeiten",
    )
}

_ALLOWED_DEGREE_KEYS = {norm(p): p for p in DEGREE_ALLOWED}
_ALLOWED_DEGREE_RES = [
    re.compile(
        r"[\s'’\-.]*".join(re.escape(w) for w in re.findall(r"[A-Za-zÄÖÜäöüß.]+", p)),
        re.IGNORECASE,
    )
    for p in DEGREE_ALLOWED
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


def is_allowed_degree(value: str) -> bool:
    return norm(value) in _ALLOWED_DEGREE_KEYS


def _collect_allowed_degree_matches(text: str) -> list[tuple[int, int, str]]:
    """All whitelisted occurrences, nested matches collapsed to the longest.

    Prevents "Abschlussarbeit" from shadowing "Wissenschaftliche
    Abschlussarbeit" (and the plural forms from being cut short).
    """
    spans = [
        (m.start(), m.end())
        for rx in _ALLOWED_DEGREE_RES
        for m in rx.finditer(text)
    ]
    spans.sort(key=lambda s: s[1] - s[0], reverse=True)
    kept: list[tuple[int, int, str]] = []
    for start, end in spans:
        if any(k_start <= start and end <= k_end for k_start, k_end, _ in kept):
            continue
        kept.append((start, end, text[start:end]))
    return kept


def allowed_degree_phrase(text: str) -> str:
    """Return the thesis-kind wording exactly as printed, if whitelisted.

    Matches flexible separators, so glued PDF text ("Bachelorthesis"),
    apostrophe/hyphen variants and missing spaces still resolve.

    Cover pages routinely mention the kind more than once ("Diese
    Bachelorarbeit widme ich ..."), so candidates are ranked: a phrase
    alone on its own line (the actual kind line) beats one embedded in a
    sentence, then the earliest occurrence wins, then the longest.
    """
    matches = _collect_allowed_degree_matches(text)
    if not matches:
        return ""
    lines = text.splitlines()
    line_starts: list[int] = []
    offset = 0
    for line in lines:
        line_starts.append(offset)
        offset += len(line) + 1

    def line_at(pos: int) -> str:
        idx = 0
        for i, start in enumerate(line_starts):
            if start <= pos:
                idx = i
            else:
                break
        return lines[idx]

    def rank(item: tuple[int, int, str]) -> tuple[int, int, int]:
        start, end, value = item
        standalone = 1 if norm(line_at(start)) == norm(value) else 0
        return (standalone, -start, end - start)

    return max(matches, key=rank)[2]


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


# OCR reads a multi-line banner by lines, so an institution's words can end
# up out of order ("Westfälische ... Hochschule", with the cities between them).
# A contiguous phrase then never matches. On an OCR-derived cover we instead
# accept the alias if all its significant words sit inside one small window of
# the cover text (one name block) - close together but in any order. The window
# keeps this from matching words scattered across two pages.
_OCR_ALIAS_WINDOW = 400


def _first_word_index(word: str, blob: str) -> int:
    """Index of the first word-bounded occurrence of `word` in a normalized
    blob (single spaces, no combining marks), or -1 if absent."""
    i, n, L = 0, len(blob), len(word)
    while True:
        j = blob.find(word, i)
        if j < 0:
            return -1
        if j > 0 and (blob[j - 1].isalnum() or blob[j - 1] in "äöüß"):
            i = j + 1
            continue
        k = j + L
        if k < n and (blob[k].isalnum() or blob[k] in "äöüß"):
            i = j + 1
            continue
        return j


def _ocr_alias_in_blob(words: list[str], blob: str) -> bool:
    sig = list(dict.fromkeys(w for w in words if len(w) >= 4))
    if not sig:
        return False
    if len(sig) == 1:
        return _first_word_index(sig[0], blob) >= 0
    hits = sorted((m.start(), m.end(), word) for word in sig
                  for m in re.finditer(rf"(?<!\w){re.escape(word)}(?!\w)", blob))
    counts: dict[str, int] = {}
    left = 0
    for right, end, word in hits:
        counts[word] = counts.get(word, 0) + 1
        while left < len(hits) and end - hits[left][0] > _OCR_ALIAS_WINDOW:
            old = hits[left][2]
            counts[old] -= 1
            if not counts[old]:
                del counts[old]
            left += 1
        if len(counts) == len(sig):
            return True
    return False


def _alias_in_blob(alias: str, blob: str, lenient: bool = False) -> bool:
    a = norm(alias)
    if not a:
        return False
    if len(a) <= 4:
        return re.search(rf"(?:^|\s){re.escape(a)}(?:\s|$)", blob) is not None
    # Institution names must be consecutive phrases, not words found
    # anywhere on the two pages (a postal city is not a second university).
    # Treat spaces, slashes and hyphens alike: Witten/Herdecke is the same
    # name as Witten Herdecke, including when printed across lines.
    words = re.split(r"[\s,.;:()_\-–—/]+", a)
    words = [w for w in words if w]
    if len(words) > 1:
        phrase = r"[\s,.;:()_\-–—/]+".join(re.escape(w) for w in words)
        if re.search(rf"(?:^|[^\wäöüß]){phrase}(?:$|[^\wäöüß])", blob):
            return True
        # Glued-text fallback (PDFs whose text layer has no spaces, e.g.
        # "derBergischenUniversitätWuppertal"): the alias must appear with
        # all spaces removed, as one contiguous run, to still count.
        compact_a = "".join(words)
        if re.search(rf"(?<!\w){re.escape(compact_a)}(?!\w)", blob):
            return True
        # OCR reading order can split a name so it is no longer contiguous;
        # only on an OCR-derived cover do we relax to "all words clustered".
        if lenient:
            return _ocr_alias_in_blob(words, blob)
        return False
    # Single long token: still require word boundaries so "technik" does
    # not match inside "automatisierungstechnik". Glued-text fallback for
    # long tokens: a city/school name like "wuppertal" may be embedded in
    # a glued compound ("...universitätwuppertal") with no space before it.
    if re.search(rf"(?:^|[^\wäöüß]){re.escape(a)}(?:$|[^\wäöüß])", blob) is not None:
        return True
    if len(a) >= 7 and re.search(
        rf"(?:universitat|university|fachhochschule|hochschule)(?:zu|in|of)?{re.escape(a)}(?!\w)", blob
    ):
        return True
    if lenient:
        return _ocr_alias_in_blob(words, blob)
    return False


# Lines that name where someone lives or was born, not the awarding
# institution. A postal address or a birthplace ("Geboren ... in Wuppertal")
# mentions a city without naming a university, so it must not count as
# evidence for that city's school — nor as the publishing year.
_ADDRESS_LINE_RE = re.compile(
    r"(?i)^\s*(?:postadresse|postanschrift|wohnadresse|wohnanschrift|anschrift)\s*:"
)
_BIRTH_LINE_RE = re.compile(
    r"(?i)^\s*(?:geboren\b|geb\.(?=\s|$)|geburtsort\b|born\b|date\s+of\s+birth)"
)
# Unlabeled address blocks: a bare "Musterstr. 12" line directly above a
# "42103 Wuppertal" line. A line opening with five digits is a postal code,
# never a year or a page number, so its city is address noise, not
# institution evidence - even when no "Postanschrift:" label introduces it.
_PLZ_LINE_RE = re.compile(r"^\s*\d{5}\s+\S")
# The street line of such a block: the first word ends in a street suffix and
# a house number follows. Only the line directly above a postal-code line is
# ever dropped on this basis.
_STREET_LINE_RE = re.compile(
    r"(?i)^\s*\S*(?:stra[ßs]e|str\.?|weg|platz|gasse|allee|ring|hof|damm|wall)\s*\.?\s+\d"
)


def cover_matching_blob(text: str) -> str:
    """Cover text reduced to institution evidence: drop address/birth lines.

    Keep the rest of the cover intact for author/title/model checks; only the
    lines that name a residence or birthplace are removed, since they mention
    a city (or a year) that is not the awarding school.
    """
    lines = text.splitlines()
    drop = [
        bool(_ADDRESS_LINE_RE.match(line) or _BIRTH_LINE_RE.match(line))
        for line in lines
    ]
    for i, line in enumerate(lines):
        if drop[i] or not _PLZ_LINE_RE.match(line):
            continue
        drop[i] = True
        if i > 0 and not drop[i - 1] and _STREET_LINE_RE.match(lines[i - 1]):
            drop[i - 1] = True
    return "\n".join(line for line, removed in zip(lines, drop) if not removed)


def resolve_university(
    cover_text: str,
    uni_id: str,
    universities: list[dict],
    templates_dir: Path | None,
    lenient: bool = False,
    evidence: CoverEvidence | None = None,
) -> tuple[dict | None, list[str], list[dict]]:
    """Validate the model's institution pick against the cover.

    The model is the identifier: it chooses a school's config id. This only
    checks that choice against the PDF - a known school is accepted only if at
    least one of its distinctive tokens (template filename or configured
    alias) appears on the cover (address/birth lines stripped). A pick with no
    such trace is rejected rather than stamped, so a misread never becomes a
    wrong foil.

    `lenient` relaxes alias matching to tolerate OCR reading order (an alias
    whose words are present but not a contiguous phrase). It is only ever used
    on an OCR-derived cover, where word order is untrustworthy; the digital
    text layer keeps the strict contiguous-phrase rule.
    """
    uni_id = (uni_id or "").strip()
    if not uni_id:
        return None, ["no_university_on_cover"], []
    uni_obj = next((u for u in universities if u.get("id") == uni_id), None)
    if uni_obj is None:
        return None, [f"university_id_not_in_catalog:{uni_id}"], []
    names = [n for n in (uni_obj.get("templates") or ([uni_obj.get("template")] if uni_obj.get("template") else [])) if n]
    if not names:
        return uni_obj, ["no_template_for_university"], [{"id": uni_id}]
    blob = norm(cover_matching_blob(cover_text))
    grounded: list[str] = []
    seen: set[str] = set()
    # Evidence may come from the foil's filename or from the school's
    # configured aliases: letterheads often print the institution's formal
    # name ("... Nordrhein-Westfalen") while the foil is named after its
    # abbreviation ("HSPV_NRW"). Dedupe case-insensitively so "hspv" (alias)
    # and "HSPV" (filename) report once.
    candidates = [a for a in (uni_obj.get("aliases") or []) if a]
    for name in names:
        candidates.extend(distinctive_filename_tokens(name))
    for cand in candidates:
        if norm(cand) in seen:
            continue
        matched = (
            any(_alias_in_blob(cand, norm(cover_matching_blob(block.text)),
                               lenient=block.source != "digital") for block in evidence.blocks)
            if evidence else _alias_in_blob(cand, blob, lenient=lenient)
        )
        if matched:
            seen.add(norm(cand))
            grounded.append(cand)
    if not grounded:
        return uni_obj, ["university_not_on_cover"], [{"id": uni_id}]
    return uni_obj, [], [{"id": uni_id, "template": uni_obj.get("template", ""), "grounded_on": grounded}]


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
{"university":"","author":"","title":"","degree_phrase":"","degree_type":"bachelor|master|dissertation|unknown","year":""}
Rules:
- author, title, year, degree_phrase MUST be copied character-for-character from COVER_TEXT. Never from METADATA.
- "university" is the id of the awarding institution, chosen from the UNIVERSITIES list in the message (the letterhead / body that issued the thesis). Output exactly that id - do NOT copy a written name. If several bodies appear, use the one that awarded the work. If none is clearly identifiable, use "".
- The author is the candidate who submitted the work. Do not use Erstgutachter, Zweitgutachter, Betreuer, or supervisor names.
- The author line may stand alone with no "vorgelegt von" / "written by". Still copy that name exactly.
- If several years appear, YOU decide which is the publishing/submission year (not a start date, copyright, or cited work). Copy that year exactly.
- degree_phrase MUST be copied character-for-character from COVER_TEXT AND be exactly one of these options: __DEGREE_OPTIONS__. If the cover shows a degree-programme name instead (e.g. "Bachelor of Arts", "Master of Science"), set degree_phrase to "" and pick degree_type. Do not rewrite it.
- Do not fix spelling. Do not translate. Do not invent.
- year must be a 4-digit year that appears in COVER_TEXT.
- degree_type is only bachelor, master, dissertation, or unknown.
- If a field is not present use "".
"""

# Keep the model's menu in sync with DEGREE_ALLOWED automatically.
SYSTEM = SYSTEM.replace("__DEGREE_OPTIONS__", _DEGREE_OPTIONS)


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
    num_thread: int | None = None,
    keep_alive: str = "30m",
    warmup_only: bool = False,
) -> str:
    options = {"temperature": 0.0, "num_predict": num_predict, "num_ctx": num_ctx}
    if num_thread:
        # Ollama's auto thread count measured ~2x slower decode than an
        # explicit setting on the CPU inference box. Keep it constant across
        # calls: changing it invalidates Ollama's prompt KV cache.
        options["num_thread"] = int(num_thread)
    payload = {
        "model": model,
        "stream": False,
        "think": think,
        "keep_alive": keep_alive,
        "options": options,
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
    body = request_json(req, timeout)
    message = body.get("message") or {}
    if not isinstance(message, dict):
        raise InvalidModelResponse("Ollama response has no message object")
    if warmup_only:
        return message.get("content") or ""
    return require_content(message.get("content"), body.get("done_reason"), message.get("thinking"))


_MODEL_REWARM_S = 12 * 3600


def warmup_model(cfg: dict) -> None:
    """Keep the model resident on the inference box and its prompt cache primed.

    A cold model load costs ~26s (load + full prefill), which the first PDF
    after an idle period would otherwise pay. The warmup call reuses the real
    SYSTEM prompt so the extraction call afterwards shares its KV prefix and
    prefills only the per-PDF part. Runs in the background; failure is logged,
    never fatal — the first PDF warms the model the old way.
    """
    mcfg = cfg.get("model", {})
    if mcfg.get("backend", "ollama") != "ollama":
        return
    try:
        call_ollama(
            mcfg.get("name", "qwen3:8b"),
            SYSTEM,
            f'UNIVERSITIES (choose exactly one id - the awarding institution - or ""):\n{_university_catalog(cfg.get("universities", []))}\nCOVER_TEXT:\nWarmup',
            mcfg.get("host", "http://[IP_ADDRESS]:11434"),
            min(int(mcfg.get("timeout", 180)), 90),
            think=model_think_value(mcfg),
            num_ctx=int(mcfg.get("num_ctx", 8192)),
            num_thread=mcfg.get("num_thread") or None,
            keep_alive=str(mcfg.get("keep_alive", "30m")),
            num_predict=1,
            warmup_only=True,
        )
        LOG.info("Model warmup done — model resident, SYSTEM prompt cached")
    except Exception as exc:
        LOG.warning("Model warmup failed (%s) — the next PDF warms it instead", exc)


def spawn_model_warmup(cfg: dict) -> None:
    def warm_when_idle():
        with _PROCESS_LOCK:
            warmup_model(cfg)
    threading.Thread(target=warm_when_idle, daemon=True).start()


# Explicit UA for hosted OpenAI-compatible endpoints: see the note in
# call_openai — the default Python-urllib UA is banned by some WAFs.
_OPENAI_USER_AGENT = "thesis-cover-watcher/1.0"


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
    think: bool | str = False,
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
    # Reasoning control. A bare top-level `thinking` flag is IGNORED by this
    # vLLM gateway (it still emits a reasoning trace), which eats the
    # max_tokens budget and leaves `content` empty. The chat-template kwarg is
    # what actually toggles the model's thinking mode on this backend.
    if isinstance(think, str) and think:
        payload["reasoning_effort"] = think
        payload["chat_template_kwargs"] = {"enable_thinking": True}
    elif think is False:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    # Some gateways sit behind Cloudflare and ban Python's default
    # `Python-urllib` User-Agent (HTTP 403 "Error 1010"). Send an explicit
    # identifying UA so the request is not blocked by that WAF rule.
    headers = {"Content-Type": "application/json", "User-Agent": _OPENAI_USER_AGENT}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    body = request_json(req, timeout)
    try:
        choice = body["choices"][0]
        message = choice["message"]
        return require_content(message.get("content"), choice.get("finish_reason"), message.get("reasoning_content"))
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise InvalidModelResponse("OpenAI response has no valid completion") from exc


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
        # Never hand back raw newlines: UPF glyph lists are single-line and a
        # control char would be encoded as an empty glyph (corrupt file).
        return re.sub(r"\s+", " ", value).strip()
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


def title_cover_span(value: str, haystack: str) -> str:
    """Ground a title, allowing sentence punctuation changes at line breaks.

    Models sometimes insert a period between a heading and subtitle. Match
    every word exactly and allow that punctuation difference only where the
    PDF actually has a line break. Return the PDF's text, not the model's
    punctuation. Same-line edits and ambiguous matches are still rejected.
    """
    exact = cover_span(value, haystack)
    if exact or not value:
        return exact

    tokens = value.split()
    parts = [r"(?<!\w)"]
    for i, token in enumerate(tokens):
        word = token.rstrip(".,;:!?") or token
        punctuation = token[len(word):]
        parts.append(re.escape(word))
        if i < len(tokens) - 1:
            parts.append(
                rf"(?:{re.escape(punctuation)}\s+|[.,;:!?]*[ \t]*\r?\n\s*)"
            )
        elif punctuation:
            parts.append(
                rf"(?:{re.escape(punctuation)}|(?=[ \t]*(?:\r?\n|$)))"
            )
    parts.append(r"(?!\w)")
    matches = list(re.finditer("".join(parts), haystack))
    if len(matches) != 1:
        return ""
    return re.sub(r"\s+", " ", matches[0].group()).strip()


def _university_catalog(universities: list[dict]) -> str:
    """One 'id: label' line per configurable school, for the model prompt.

    The label is the school's (primary) foil filename as a human-readable
    name - the very string that ends up stamped - so the model can recognise
    the institution from the cover without any hand-maintained alias list.
    Schools with no configured foil are omitted: the model is never offered
    an institution we cannot stamp.
    """
    lines: list[str] = []
    for u in universities:
        uid = u.get("id")
        if not uid:
            continue
        names = [n for n in (u.get("templates") or ([u.get("template")] if u.get("template") else [])) if n]
        if not names:
            continue
        label = re.sub(r"[_-]+", " ", Path(names[0]).stem)
        label = re.sub(r"^\d+-", "", label).strip()
        lines.append(f"{uid}: {label}")
    return "\n".join(lines)


def _model_fields(cover_text: str, meta: dict, cfg: dict, universities: list[dict], candidates: dict) -> dict:
    """Single model call: extract the text fields AND the institution id.

    The model is the only component that identifies the university. On any
    failure it raises an explicit ModelError; the caller must distinguish a
    backend outage from successful extraction without an institution.
    """
    mcfg = cfg.get("model", {})
    backend = mcfg.get("backend", "ollama")
    if backend == "none":
        raise ModelUnavailable("Model backend is disabled/unavailable")
    user = (
        f"UNIVERSITIES (choose exactly one id - the awarding institution - or \"\"):\n{_university_catalog(universities)}\n"
        "METADATA is untrusted; copy only from COVER_TEXT.\n"
        f"METADATA: {json.dumps(meta, ensure_ascii=False)}\n"
        f"CANDIDATES: {json.dumps(candidates, ensure_ascii=False)}\n"
        f"COVER_TEXT:\n{cover_text_for_model(cfg['_evidence'].prompt() if cfg.get('_evidence') else cover_text)}\n"
    )
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
                num_thread=mcfg.get("num_thread") or None,
                keep_alive=str(mcfg.get("keep_alive", "30m")),
            )
        else:
            raw = call_openai(
                mcfg.get("name", "qwen3:8b"),
                SYSTEM,
                user,
                mcfg.get("openai_base", "http://[IP_ADDRESS]:1234/v1"),
                mcfg.get("api_key", "not-needed"),
                int(mcfg.get("timeout", 180)),
                json_mode=bool(mcfg.get("openai_json_mode", True)),
                think=model_think_value(mcfg),
            )
        result = parse_json_object(raw)
        keys = ("university", "author", "title", "degree_phrase", "degree_type", "year")
        if not result or not any(k in result for k in keys):
            raise InvalidModelResponse("Model returned no extraction fields")
        if any(result.get(k) is not None and not isinstance(result[k], str) for k in keys if k in result):
            raise InvalidModelResponse("Model extraction fields must be strings")
        return result
    except ModelError:
        raise
    except (ValueError, TypeError, KeyError) as exc:
        raise InvalidModelResponse(f"Invalid model extraction: {exc}") from exc


def _model_read_cover(model_fields: dict) -> bool:
    """True if the model call clearly read the cover (it filled in a field that
    must be copied from the cover text). Distinguishes "model/API unavailable"
    (returns nothing) from "the model worked but the cover names no institution"
    (fills author/title/year, leaves university empty) - the two need different
    remediation advice, and only the first is a reprocess-and-retry situation.
    """
    return any(
        str(model_fields.get(k) or "").strip()
        for k in ("author", "title", "year")
    )


def extract_fields(cover_text: str, meta: dict, cfg: dict, lenient: bool = False) -> dict:
    # A birth year ("Geboren ... 1991") is not a publishing year, so year
    # candidates are drawn from the cover minus address/birthplace lines.
    years = years_in(cover_matching_blob(cover_text))
    labelled_authors = author_hints(cover_text)
    degree = guess_degree(cover_text)
    candidates = {"authors": labelled_authors, "years": years, "degree_guess": degree}

    universities = cfg.get("universities", [])
    templates_dir = Path(cfg["templates_dir"]) if cfg.get("templates_dir") else None

    # The model is the identifier: one call extracts the text fields and the
    # awarding institution's config id. No alias or filename matching is used.
    # lenient=True only changes how the model's institution pick is grounded
    # against the cover (order-tolerant) - it is used when that cover text is
    # an OCR render whose reading order cannot be trusted. Text fields are
    # unaffected.
    outcome = ModelOutcome()
    try:
        model_fields = _model_fields(cover_text, meta, cfg, universities, candidates)
    except ModelError as exc:
        model_fields = {}
        outcome = ModelOutcome("unavailable" if isinstance(exc, ModelUnavailable) else "invalid_response", str(exc))
        LOG.warning("Model extraction %s: %s", outcome.status, exc)

    hay = cover_text
    risks: list[str] = []
    if outcome.status != "ok":
        risks.append("model_" + outcome.status)

    author, ar = resolve_author(
        hay, str(model_fields.get("author") or "").strip(), labelled_authors
    )
    risks.extend(ar)
    if author:
        author = cover_span(author, hay) or author

    model_title = str(model_fields.get("title") or "").strip()
    title = title_cover_span(model_title, hay)
    if model_title and not title:
        risks.append("title_not_on_cover")
        LOG.warning("Model title did not match the cover — TITLE variants will be skipped")

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
    if not (
        model_phrase
        and is_allowed_degree(model_phrase)
        and verbatim_ok(model_phrase, hay)
    ):
        model_phrase = ""

    # The model names the institution by id; the cover only grounds (or vetoes)
    # that pick. Which foil to stamp is decided purely from config below.
    uni_id = str(model_fields.get("university") or "").strip()
    uni, ur, uni_matches = resolve_university(cover_text, uni_id, universities, templates_dir,
                                             lenient=lenient, evidence=cfg.get("_evidence"))
    risks.extend(ur)

    notes: list[str] = []
    if "title_not_on_cover" in risks:
        notes.append("Title omitted: the model's title could not be matched to the PDF cover.")
    institution_notes: list[str] = []
    if "university_not_on_cover" in risks:
        institution_notes.append(
            f"The model identified '{uni_id}', but no configured name evidence "
            "appears on the cover - not stamped. Check the cover, or fix "
            f"the '{uni_id}' entry's template in config.json."
        )
    unknown_id = next((r for r in risks if r.startswith("university_id_not_in_catalog")), None)
    if unknown_id:
        institution_notes.append(
            f"The model returned institution id '{unknown_id.split(':', 1)[1]}', "
            "which is not in config.json - add it there, then reprocess."
        )
    if "no_university_on_cover" in risks:
        if outcome.status == "ok":
            institution_notes.append(
                "No awarding institution was identified in the extracted cover text. "
                "Review the original PDF, its logo/text extraction, and the configured "
                "institution names before reprocessing or stamping manually."
            )
        else:
            institution_notes.append(
                "The model did not identify an awarding institution (the model/API "
                "may be unavailable). Reprocess once the model is reachable."
            )

    labels = cfg.get("degree_labels", {})
    # The cover is ground truth. The model can latch onto a stray mention
    # ("Diese Bachelorarbeit ...") while the kind line says something else.
    printed = allowed_degree_phrase(cover_text) or model_phrase
    if printed:
        if norm(printed) in DEGREE_AGNOSTIC and dt in labels and dt != "unknown":
            # The cover says only "Thesis"/"Abschlussarbeit" but the degree is
            # known (e.g. "Bachelor of Arts" beneath it): stamp the concrete
            # degree label instead of the vague word.
            degree_text = labels[dt]
            risks.append("degree_agnostic_wording_degree_inferred")
        else:
            degree_text = printed
    else:
        degree_text = labels.get(dt, labels.get("unknown", "Thesis"))
        risks.append("degree_inferred_not_on_cover")

    # A university that is unknown, ungrounded, or foiless is never stamped:
    # _process_pdf_inner raises on these before any foil is written.
    ok_to_stamp = can_stamp({"risks": risks, "university_id": uni_id if uni else "", "author": author, "year": year})

    return {
        "university_id": uni["id"] if uni else "",
        "university_template": "",
        "university_raw": uni_id,
        "university_matches": uni_matches,
        "author": author,
        "title": title,
        "degree_type": dt,
        "degree_text": degree_text,
        "year": year,
        "candidates": candidates,
        "risks": risks,
        "notes": notes + institution_notes,
        "institution_notes": institution_notes,
        "ok_to_stamp": ok_to_stamp,
        "model_raw": model_fields,
        "model_outcome": asdict(outcome),
    }


def _institution_unresolved(risks: list[str]) -> bool:
    """True when the model's institution pick is not grounded on the cover: it
    named none, named one not in the catalog, or named one with no matching
    trace. These are exactly the cases a second OCR read can still rescue (a
    name that only a rendered banner/logo carries, not the text layer)."""
    if any(r.startswith("model_") for r in risks):
        return False
    return any(
        r in ("no_university_on_cover", "university_not_on_cover")
        or r.startswith("university_id_not_in_catalog:")
        for r in risks
    )


def _rescue_university_from_ocr(pdf_path: Path, meta: dict, cfg: dict, fields: dict) -> tuple[dict, bool]:
    """Second chance: read the institution from a full-page OCR render.

    The digital text layer carries the typed cover fields but often not the
    school name - that is frequently a raster banner/logo the text layer cannot
    carry. When the digital read therefore yields no grounded institution, this
    renders the cover, asks the model to name the institution from THAT text,
    and grounds the pick with the order-tolerant matcher. Only the institution
    is re-read; the trusted digital author/title/year in `fields` are kept.

    Returns (fields, used_ocr); used_ocr is True once the OCR model read was
    attempted, so the caller can tell "cover names no institution in any form
    we can read" from "the model was down".
    """
    universities = cfg.get("universities", [])
    templates_dir = Path(cfg["templates_dir"]) if cfg.get("templates_dir") else None
    ocr_started = time.monotonic()
    try:
        ocr_result = extract_ocr(
                pdf_path,
                2,
                int(cfg.get("ocr_dpi", 300)),
                cfg.get("ocr_lang", "deu+eng"),
                cfg,
                return_evidence=True,
            )
        ocr_text = compact_cover(ocr_result.text if isinstance(ocr_result, CoverEvidence) else ocr_result)
    except Exception as exc:
        LOG.warning("OCR second-read unavailable (%s) - no rescue possible", exc)
        return fields, False
    if len(ocr_text.strip()) < int(cfg.get("ocr_min_chars", 80)):
        LOG.info("OCR second-read produced no usable text - no rescue possible")
        return fields, False
    fields.setdefault("extraction_timings", {})["rescue_ocr_seconds"] = time.monotonic() - ocr_started
    try:
        ocr_cfg = {k: v for k, v in cfg.items() if k != "_evidence"}
        if isinstance(ocr_result, CoverEvidence):
            ocr_cfg["_evidence"] = ocr_result
        ocr_fields = _model_fields(ocr_text, meta, ocr_cfg, universities, fields.get("candidates") or {})
    except ModelError as exc:
        status = "unavailable" if isinstance(exc, ModelUnavailable) else "invalid_response"
        fields["model_outcome"] = asdict(ModelOutcome(status, str(exc)))
        fields["risks"].append("model_" + status)
        fields["ok_to_stamp"] = False
        return fields, True
    ocr_uni_id = str(ocr_fields.get("university") or "").strip()
    LOG.info("OCR second-read: model picked %r from the rendered cover", ocr_uni_id)
    uni, ur, uni_matches = resolve_university(
        ocr_text, ocr_uni_id, universities, templates_dir, lenient=True,
        evidence=ocr_result if isinstance(ocr_result, CoverEvidence) else None,
    )

    fields["risks"] = [
        r for r in fields.get("risks", [])
        if not (r in ("no_university_on_cover", "university_not_on_cover")
                or r.startswith("university_id_not_in_catalog:"))
    ]
    fields["risks"].extend(ur)
    fields["university_raw"] = ocr_uni_id or fields.get("university_raw", "")
    fields["ocr_escalated"] = True
    institution_notes = []

    if uni is not None and not ur:
        fields["university_id"] = uni["id"]
        fields["university_matches"] = uni_matches
        institution_notes.append(
            "The awarding institution is not in the PDF's text layer (it is a "
            "banner/logo). It was recovered from a full-page OCR of the cover and "
            "confirmed against the school's configured name."
        )
    elif ocr_uni_id:
        institution_notes.append(
            "The cover was read from its text layer and again by full-page OCR; the "
            f"OCR read named '{ocr_uni_id}', which could not be confirmed on the "
            "cover, so nothing was stamped."
        )
    else:
        institution_notes.append(
            "The cover was read from its text layer and again by full-page OCR; "
            "neither extraction identified an institution we can match, so none was stamped."
        )
    set_institution_notes(fields, institution_notes)
    fields["ok_to_stamp"] = can_stamp(fields)
    return fields, True


# ---------------------------------------------------------------------------
# UPF text blocks
# ---------------------------------------------------------------------------

def logo_block_indices(upf: str, blocks: list[TextBlock]) -> set[int]:
    """Blocks that sit inside a background field (PrintArea) holding the logo.

    Templates normally have one more background field than input fields; the
    extra one carries the logo. Some templates instead put an extra input
    (usually the university name) onto that logo field. Text living there is
    branding — it must never receive the title role.
    """
    out: set[int] = set()
    for pm in re.finditer(r"Object:PrintArea\s*\{", upf):
        open_b = upf.find("{", pm.end() - 1)
        close_b = _matching_brace(upf, open_b)
        if close_b < 0 or close_b - open_b > 3_000_000:
            continue  # brace desync or absurd span — do not trust it
        span = upf[open_b:close_b]
        if "Object:Image" not in span:
            continue
        for i, b in enumerate(blocks):
            if open_b <= b.start and b.end <= close_b:
                out.add(i)
    return out


def classify_blocks(
    blocks: list[TextBlock], placeholders: dict, logo_idx: frozenset[int] | set[int] = frozenset()
) -> dict[str, int]:
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
        # Title: never fabricate a positional guess. The only candidates are
        # blocks not already claimed as degree/author and not sitting on the
        # logo background field (branding text there — e.g. FOM's university
        # name — must never receive the title). If several remain, the longest
        # one is the title sample; the rest fall back to author/year.
        claimed = set(roles.values())
        cand = [i for i in usable_idx if i not in claimed and i not in logo_idx]
        if cand:
            roles["title"] = max(cand, key=lambda i: len(blocks[i].joined()))
        if "author_year" not in roles:
            rem = [i for i in usable_idx if i not in set(roles.values())]
            if rem:
                roles["author_year"] = rem[0]
    return roles


def fill_upf(template_text: str, fields: dict, cfg: dict) -> str:
    blocks = find_text_blocks(template_text)
    if not blocks:
        raise ValueError("No Object:TextDesignElement found in UPF")

    fillable = [b for b in blocks if not b.is_empty()]
    logo_idx = logo_block_indices(template_text, blocks)
    roles = classify_blocks(blocks, cfg.get("placeholders", {}), logo_idx)
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

    def claim(idx: int, lines: list[str], role: str) -> None:
        # Two roles landing on one field previously let the title silently
        # overwrite the degree text. Never allow that.
        if idx in updates:
            raise ValueError(f"UPF role collision: {role} targets already-filled field {idx}")
        updates[idx] = lines

    if "degree" in roles:
        claim(roles["degree"], [fields["degree_text"]], "degree")

    if "title" in roles and fields.get("title"):
        claim(roles["title"], [fields["title"]], "title")

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
        claim(idx, new_lines, "author_year")

    if fields.get("title") and "title" not in roles:
        # Template has no title placeholder (e.g. only degree + author fields).
        # Leave the title off rather than stamping it over another field.
        LOG.warning("Template has no title field — title left off the cover")
        risks = fields.setdefault("risks", [])
        if "no_title_field_in_template" not in risks:
            risks.append("no_title_field_in_template")

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
    """Existing plain templates explicitly assigned to this institution.

    `templates` lists approved variants; without it only `template` belongs
    to the school. A filename/city similarity is not proof of membership.
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

    names = (
        uni.get("templates", [uni.get("template")])
        if uni is not None else [chosen]
    )
    if chosen and chosen in names:
        add(templates_dir / chosen)
    for name in names:
        if name:
            add(templates_dir / name)
    return out


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
    folders.append(Path(cfg.get("local_fail_dir") or _local_fail_dir()))
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
    # Reporting must remain fast and usable even when inference is unavailable.
    model_note = _fallback_fail_note(facts["error"], facts["fields"])
    body = [
        f"FAIL  {pdf_path.name}",
        f"time  {stamp}",
        f"error {facts['error']}",
        "",
        "--- extracted ---",
        json.dumps(facts["fields"], ensure_ascii=False, indent=2),
        "",
        "--- operator report ---",
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
    # The prägen app requires CRLF line endings; writing LF-only produces
    # files it cannot open. Normalize, then force CRLF before writing.
    filled = filled.replace("\r\n", "\n").replace("\n", "\r\n")
    data = filled.encode("utf-8")
    job = cfg.get("_job")
    retry_io(lambda: job.publish(out_path, data) if job else atomic_write(out_path, data),
             what=f"writing {out_path.name}")
    LOG.info("Wrote %s from %s", out_path.name, template_path.name)
    return out_path


_TIMING_WARN_S: dict[str, float] = {"read": 10.0, "model": 15.0, "write": 5.0, "total": 30.0}


def _log_timings(
    read: float, model: float, write: float, total: float, source: str, cfg: dict
) -> None:
    """One-line per-PDF stage timing so slow runs diagnose themselves."""
    timing = cfg.get("_timings", {})
    settle_txt = ", ".join(f"{key} {timing[key]:.1f}s" for key in ("queue", "settle", "stage") if key in timing)
    settle_txt = settle_txt + ", " if settle_txt else ""
    LOG.info(
        "timings: %sread %.1fs (source=%s), model %.1fs, write %.1fs, total %.1fs",
        settle_txt, read, source, model, write, total,
    )
    warn = {**_TIMING_WARN_S, **(cfg.get("timing_warn_seconds") or {})}
    for name, val in (("read", read), ("model", model), ("write", write), ("total", total)):
        threshold = warn.get(name)
        if threshold is not None and val > threshold:
            LOG.warning("Slow %s stage: %.1fs (threshold %.0fs)", name, val, threshold)


def process_pdf(pdf_path: Path, cfg: dict) -> list[Path]:
    LOG.info("Processing %s", pdf_path)
    ctx: dict = {"text": "", "source": "", "fields": {}}
    started = time.monotonic()
    try:
        with job_lock(pdf_path, cfg), staged_pdf(pdf_path) as (local_pdf, digest):
            job = JobState(pdf_path, digest, cfg)
            if job.record["completed"]:
                LOG.info("Already generated %s — retrying archival only", pdf_path.name)
                return [Path(p) for p in job.record["outputs"]]
            runtime = {**cfg, "_job": job, "_source_pdf": str(pdf_path),
                       "_timings": {**cfg.get("_timings", {}), "stage": time.monotonic() - started}}
            with capture_inference():
                written = _process_pdf_inner(local_pdf, runtime, ctx)
            job.complete(written)
            LOG.info("Job wall time including staging/checkpoints: %.2fs", time.monotonic() - started)
            return written
    except (ModelUnavailable, RetryableJobError):
        # A backend outage is retryable; do not create repeated failure files.
        LOG.warning("Model unavailable; leaving %s queued for retry", pdf_path.name)
        raise
    except OSError as exc:
        raise RetryableJobError(f"Job I/O interrupted: {exc}") from exc
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
                dest = Path(cfg.get("local_fail_dir") or _local_fail_dir()) / (
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


def _prepare_pdf(pdf_path: Path, cfg: dict, ctx: dict) -> tuple[dict, str, float, float]:
    t0 = time.monotonic()
    extraction = get_cover_text(pdf_path, cfg)
    text, source = extraction
    if isinstance(extraction, CoverEvidence):
        cfg = {**cfg, "_evidence": extraction}
        ctx["extraction"] = {"blocks": [asdict(b) for b in extraction.blocks], "timings": extraction.timings}
    t_read = time.monotonic() - t0
    ctx["text"], ctx["source"] = text, source
    LOG.info("Cover text source=%s chars=%s", source, len(text))
    if not text.strip():
        raise RuntimeError("No text from first two pages (digital + OCR empty)")

    meta = extraction.metadata if isinstance(extraction, CoverEvidence) else pdf_metadata(pdf_path)
    t0 = time.monotonic()
    # The cover text is the digital layer unless it was too short (then
    # get_cover_text already OCR'd it). An OCR-derived cover has untrustworthy
    # word order, so the institution is grounded order-tolerantly; the digital
    # layer keeps the strict contiguous-phrase rule.
    fields = extract_fields(text, meta, cfg, lenient=(source == "ocr"))
    if isinstance(extraction, CoverEvidence):
        fields["extraction_timings"] = dict(extraction.timings)
        fields["extraction_sources"] = [{"page": b.page, "source": b.source} for b in extraction.blocks]
    t_model = time.monotonic() - t0
    ctx["fields"] = fields
    LOG.info("Resolved fields: %s", {k: v for k, v in fields.items() if k not in {"candidates", "model_raw"}})
    if fields.get("risks"):
        LOG.warning("Risks: %s  ok_to_stamp=%s", fields["risks"], fields.get("ok_to_stamp"))

    if "model_unavailable" in fields.get("risks", []):
        raise ModelUnavailable(fields["model_outcome"]["error"])
    if "model_invalid_response" in fields.get("risks", []):
        raise InvalidModelResponse(fields["model_outcome"]["error"])

    # The digital layer can carry the typed fields (author/title/year) yet name
    # no institution - the school name often lives only in a raster banner the
    # text layer cannot hold. Then take one second chance: read the institution
    # from a full-page OCR of the cover. The typed fields stay the trusted
    # digital ones; only the institution is re-read and grounded leniently.
    if source.startswith("digital") and _institution_unresolved(fields.get("risks", [])):
        LOG.info("No institution in the digital layer - escalating to a full-page OCR read")
        t_ocr0 = time.monotonic()
        fields, ocr_tried = _rescue_university_from_ocr(pdf_path, meta, cfg, fields)
        ocr_seconds = fields.get("extraction_timings", {}).get("rescue_ocr_seconds", 0)
        t_model += time.monotonic() - t_ocr0 - ocr_seconds
        t_read += ocr_seconds
        ctx["fields"] = fields
        if ocr_tried:
            source = "digital+ocr"
            ctx["source"] = source
            LOG.info("Cover text source now=%s", source)
            if fields.get("risks"):
                LOG.warning("Risks after OCR: %s  ok_to_stamp=%s", fields["risks"], fields.get("ok_to_stamp"))

    if "model_unavailable" in fields.get("risks", []):
        raise ModelUnavailable(fields["model_outcome"]["error"])
    if "model_invalid_response" in fields.get("risks", []):
        raise InvalidModelResponse(fields["model_outcome"]["error"])

    unknown_id = next(
        (r for r in fields.get("risks", []) if r.startswith("university_id_not_in_catalog:")), None
    )
    if unknown_id:
        raise RuntimeError(
            f"Model returned university id '{unknown_id.split(':', 1)[1]}', which is not in config.json. "
            "Add that institution (id + template) to config.json, then reprocess. Refusing to stamp."
        )
    if "university_not_on_cover" in fields.get("risks", []):
        where = "the rendered cover (full-page OCR)" if fields.get("ocr_escalated") else "the cover"
        raise RuntimeError(
            f"Model identified '{fields['university_raw']}' but no part of that school's name "
            f"appears on {where}. Refusing to stamp a guessed foil - review the PDF."
        )
    if "no_university_on_cover" in fields.get("risks", []):
        if fields.get("model_outcome", {}).get("status") == "ok" or _model_read_cover(fields.get("model_raw") or {}):
            if fields.get("ocr_escalated"):
                raise RuntimeError(
                    "No awarding institution identified: the cover was read from its text "
                    "layer and again with full-page OCR, and neither extraction identified "
                    "a university we can match. Review the original cover and configuration, "
                    "or stamp manually. Refusing "
                    "to stamp."
                )
            raise RuntimeError(
                "No awarding institution identified in the extracted cover text. "
                "Review the original cover, logo extraction, and configuration. Refusing to stamp."
            )
        raise RuntimeError(
            f"No awarding institution identified (model returned {fields['university_raw']!r}; "
            "the model/API may be unavailable). Reprocess once the model is reachable."
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

    if "no_template_for_university" in fields.get("risks", []):
        raise RuntimeError(
            f"No configured template is available for {fields['university_id']!r}. "
            "Its templates were removed or renamed; update this institution's "
            "template/templates entries in config.json."
        )

    fields["inference_metrics"] = inference_metrics()
    fields["job_timing"] = cfg.get("_timings", {})
    return fields, source, t_read, t_model


def _process_pdf_inner(pdf_path: Path, cfg: dict, ctx: dict) -> list[Path]:
    t_start = time.monotonic()
    job = cfg.get("_job")
    prepared = job.prepared() if job else None
    if prepared:
        fields, source = prepared["fields"], prepared["source"]
        ctx.update(text=prepared["text"], source=source, fields=fields)
        t_read = t_model = 0.0
        LOG.info("Resuming prepared output publication for %s", pdf_path.name)
    else:
        fields, source, t_read, t_model = _prepare_pdf(pdf_path, cfg, ctx)
        if job:
            job.prepared({"fields": fields, "source": source, "text": ctx["text"]})

    if not can_stamp(fields):
        raise RuntimeError("Validated fields are not safe to stamp: " + ", ".join(fields.get("risks", [])))

    templates_dir = Path(cfg["templates_dir"])
    uni_obj = next(
        (u for u in cfg.get("universities", []) if u.get("id") == fields["university_id"]),
        None,
    )
    variants = template_variants_for(uni_obj, fields["university_template"], templates_dir)
    if not variants:
        if not fields["university_template"]:
            raise RuntimeError(
                f"No approved .upf file exists for institution {fields['university_raw']!r}. "
                "Check its configured template filenames and the template share."
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
    t_write0 = time.monotonic()
    job_suffix = "_" + job.record["generation"] if job else ""
    # A journal generation owns its names even before any variant is published.
    # A deliberate --force run receives a fresh generation.
    def paths_for(base):
        if job:
            stem = base + job_suffix
            return out_dir / f"{stem}.upf", out_dir / f"{stem}_TITLE.upf", out_dir / f"{stem}.json"
        return allocate_output_paths(out_dir, base)

    _, _, sidecar = paths_for(output_base_name(fields))
    written: list[Path] = []
    outputs: list[dict] = []
    variant_errors: list[str] = []
    publication_io_errors: list[str] = []

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
        plain_path, _tp, _sc = paths_for(base)
        try:
            path = _write_upf(tpl, fields, cfg, plain_path)
            written.append(path)
            outputs.append({"variant": "plain", "template": tpl.name, "path": str(path)})
        except Exception as exc:
            variant_errors.append(f"plain ({tpl.name}): {exc}")
            if isinstance(exc, OSError):
                publication_io_errors.append(str(exc))
            LOG.exception("Failed to fill template %s", tpl)

        if write_title:
            _u, titled = find_template_pair(tpl.name, templates_dir)
            if titled is not None and not fields.get("title"):
                # Without a real title the foil would keep the "Titel"
                # placeholder dummy — skip it and say why.
                variant_errors.append(f"title ({titled.name}): skipped — no title extracted")
                LOG.info("No title extracted — skipping TITLE variant %s", titled.name)
            elif titled is not None:
                title_base = output_base_name(fields)
                if single:
                    # One template: mark the title foil with a stable
                    # "_Titel" suffix. Reusing the plain base would collide
                    # with the just-written plain cover and fall back to
                    # "_1", so single-template schools would end up with
                    # "…_1.upf" while multi-template ones got "…_Titel.upf".
                    title_base = f"{title_base}_Titel"
                else:
                    title_base = f"{title_base}_{safe_stem(titled.stem)}"
                title_path, _t2, _s2 = paths_for(title_base)
                try:
                    path = _write_upf(titled, fields, cfg, title_path)
                    written.append(path)
                    outputs.append({"variant": "title", "template": titled.name, "path": str(path)})
                except Exception as exc:
                    variant_errors.append(f"title ({titled.name}): {exc}")
                    if isinstance(exc, OSError):
                        publication_io_errors.append(str(exc))
                    LOG.exception("Failed to fill title template %s", titled)
            elif single:
                variant_errors.append(f"no TITEL/TITLE sibling next to {tpl.name}")
                LOG.info("No TITEL/TITLE sibling for family of %s", tpl.name)

    if publication_io_errors:
        raise RetryableJobError("Output publication interrupted: " + "; ".join(publication_io_errors))
    if not written:
        raise RuntimeError("Could not write any UPF: " + "; ".join(variant_errors))

    sidecar_data = json.dumps(
                {
                    "pdf": cfg.get("_source_pdf", str(pdf_path)),
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
            ).encode("utf-8")
    retry_io(lambda: job.publish(sidecar, sidecar_data) if job else atomic_write(sidecar, sidecar_data),
             what=f"writing {sidecar.name}")
    if fields.get("notes"):
        notes_path = sidecar.with_name(sidecar.stem + "_NOTES.txt")
        try:
            atomic_write(notes_path, ("\n\n".join(fields["notes"]).strip() + "\n").encode("utf-8"))
            LOG.info("Wrote operator notes %s", notes_path.name)
        except OSError:
            LOG.exception("Optional notes could not be written; UPFs and sidecar are complete")
    if variant_errors:
        LOG.warning("Variant issues: %s", variant_errors)
    if not fields.get("ok_to_stamp", True):
        LOG.warning("Wrote %s but ok_to_stamp=false — review sidecar before production", written)
    if fields.get("ok_to_stamp", True):
        notify(
            cfg,
            "Deckblatt erstellt",
            f"{fields['author']} ({fields['year']}) — "
            f"{fields.get('university_raw') or fields.get('university_id') or '?'} — "
            f"{len(written)} Vorlage(n)",
            app="Thesis Cover – fertig",
        )
    _log_timings(
        t_read, t_model, time.monotonic() - t_write0, time.monotonic() - t_start,
        source, cfg,
    )
    return written


def move_aside(pdf_path: Path, dest_dir: Path) -> Path | None:
    """Move a processed/failed PDF into dest_dir.

    Idempotent: if the source is already gone (the event watcher and the
    periodic rescan can both race for the same file, and the first one to
    finish moves it) this is a no-op rather than a crash.
    """
    if not pdf_path.exists():
        LOG.info("Nothing to move — %s is already gone", pdf_path.name)
        return None
    ensure_dirs(dest_dir)
    target = dest_dir / pdf_path.name
    if target.exists():
        target = dest_dir / f"{pdf_path.stem}_{int(time.time())}{pdf_path.suffix}"
    try:
        return retry_io(lambda: shutil.move(str(pdf_path), str(target)), what=f"moving {pdf_path.name}")
    except FileNotFoundError:
        if not pdf_path.exists():
            LOG.info("Source vanished while moving %s — already handled elsewhere", pdf_path.name)
            return None
        raise


_PROCESS_LOCK = threading.Lock()

_NOTIFY_MISSING_NOTED = False


def notify(cfg: dict, summary: str, body: str, urgent: bool = False, app: str = "Thesis Cover") -> None:
    """Best-effort desktop popup (notify-send) for the shop screen.

    A missing binary or a failed send must never affect cover production —
    this is pure convenience, so every failure path is silent.
    Disable with "notify": false in config.json.

    Each popup type uses its own app name: GNOME coalesces popups that
    share an app name into one (the newest hides the oldest), which would
    block a fresh result popup behind an un-dismissed start popup.
    Distinct names let the types coexist, while a new popup of the same
    type still replaces an older un-dismissed one.
    """
    global _NOTIFY_MISSING_NOTED
    if not cfg.get("notify", True):
        return
    if not shutil.which("notify-send"):
        if not _NOTIFY_MISSING_NOTED:
            _NOTIFY_MISSING_NOTED = True
            LOG.info("notify-send not found — desktop notifications disabled")
        return
    try:
        subprocess.run(
            ["notify-send", "--app-name", app, summary, body]
            + (["--urgency", "critical"] if urgent else []),
            timeout=5,
            check=False,
        )
    except Exception:
        pass


def handle_pdf(pdf_path: Path, cfg: dict, origin: str, timings=None) -> bool:
    """Process one PDF and move it aside; never raises.

    Both the event watcher (observer thread) and scan_existing (main thread)
    can hand us the same file. Serializing on a lock plus re-checking that
    the file still exists makes the second caller a no-op, so a PDF is only
    processed once and no move race can kill the process.
    """
    with _PROCESS_LOCK:
        if not pdf_path.exists():
            LOG.info("Skipping %s (%s) — already handled", pdf_path.name, origin)
            return True
        cfg = {**cfg, "_timings": timings or {}}
        notify(cfg, "Cover wird bearbeitet", pdf_path.name, app="Thesis Cover – läuft")
        dest: str | None = None
        try:
            initial = pdf_path.stat()
            process_pdf(pdf_path, cfg)
            if not pdf_path.exists():
                return True
            current = pdf_path.stat()
            if (initial.st_size, initial.st_mtime_ns) != (current.st_size, current.st_mtime_ns):
                LOG.info("Source was replaced during processing; queueing its new contents")
                return False
            dest = cfg.get("processed_dir")
        except (ModelUnavailable, RetryableJobError) as exc:
            LOG.warning("Deferring %s: %s", pdf_path.name, exc)
            return False
        except Exception as exc:
            LOG.error("Failed on %s (%s): %s", pdf_path, origin, exc)
            notify(
                cfg,
                "Fehler – kein Cover erstellt",
                f"{pdf_path.name}: {str(exc).splitlines()[0][:200]}",
                urgent=True,
                app="Thesis Cover – Fehler",
            )
            dest = cfg.get("failed_dir")
        if not dest:
            return True
        try:
            move_aside(pdf_path, Path(dest))
        except Exception:
            # A failure to file the PDF away must never take the watcher down.
            LOG.exception("Could not move %s to %s — leaving it in the watch folder", pdf_path, dest)
            return False
        return True


class PdfHandler(FileSystemEventHandler):
    def __init__(self, cfg: dict, work: PdfQueue):
        self.cfg = cfg
        self.work = work

    def on_created(self, event):  # noqa: N802
        if getattr(event, "is_directory", False):
            return
        self._maybe(Path(event.src_path))

    def on_moved(self, event):  # noqa: N802
        if getattr(event, "is_directory", False):
            return
        self._maybe(Path(event.dest_path))

    def _maybe(self, path: Path) -> None:
        # Any exception here would kill the watchdog emitter thread and leave
        # the process running but no longer watching. Never let one escape.
        try:
            self._handle_event(path)
        except Exception:
            LOG.exception("Unhandled error while reacting to event for %s", path)

    def _handle_event(self, path: Path) -> None:
        if path.suffix.lower() != ".pdf":
            return
        self.work.submit(path, "watch")


def scan_existing(cfg: dict, work: PdfQueue) -> None:
    watch = Path(cfg["watch_dir"])
    if not watch.is_dir():
        LOG.error("watch_dir not found: %s", watch)
        return
    unique = sorted(p for p in watch.iterdir() if p.suffix.lower() == ".pdf" and p.is_file())
    if not unique:
        LOG.info("No PDFs in %s", watch)
        return
    for pdf in unique:
        work.submit(pdf, "scan")


def _watcher_dead(observer) -> bool:
    """True when the observer or any of its emitter threads has died.

    watchdog's is_alive() only covers the dispatcher thread. On an AFP
    dropout the polling emitter dies with an unhandled OSError while the
    observer itself keeps looking healthy — the watcher then goes deaf and
    PDFs are only picked up by the periodic rescan, minutes later.
    """
    if not observer.is_alive():
        return True
    return any(not e.is_alive() for e in observer.emitters)


def _start_observer(cfg: dict, handler: PdfHandler):
    """Create and start the file-system observer for the watch dir."""
    use_polling = bool(cfg.get("watch_polling", True))
    if use_polling and PollingObserver is not None:
        observer = PollingObserver(timeout=float(cfg.get("watch_poll_interval", 5.0)))
        LOG.info("Using polling observer (interval=%ss) for network share", cfg.get("watch_poll_interval", 5.0))
    elif Observer is not None:
        observer = Observer()
        if use_polling:
            LOG.warning("PollingObserver unavailable — falling back to inotify Observer (may miss events on network shares)")
    else:
        return None
    observer.schedule(handler, str(Path(cfg["watch_dir"])), recursive=False)
    observer.start()
    return observer


def _heal_observer(cfg: dict, handler: PdfHandler, observer, backoff: float):
    """Replace a dead observer in-process (AFP dropouts kill the emitter).

    Returns the new observer, or the old one if recreation failed. `backoff`
    limits hot-looping when the share stays down; callers double it per failed
    heal and reset it once a recreated watcher stays alive.
    """
    LOG.warning(
        "File watcher died (AFP dropout?) — recreating in-process in %.0fs "
        "(was: observer alive=%s, emitters=%s)",
        backoff, observer.is_alive(),
        [(type(e).__name__, e.is_alive()) for e in observer.emitters],
    )
    time.sleep(min(backoff, 60.0))
    try:
        observer.stop()
    except Exception:
        pass
    try:
        fresh = _start_observer(cfg, handler)
    except Exception:
        # e.g. the share is still unmounted: keep the old (dead) observer so
        # the caller exits and systemd brings us back once it is up again.
        LOG.exception("Could not recreate the file watcher — exiting for systemd restart")
        return observer
    if fresh is None:
        LOG.error("Could not recreate the file watcher — exiting for systemd restart")
        return observer
    LOG.info("File watcher recreated — back to live event watching")
    return fresh


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=Path("config.json"))
    ap.add_argument("--once", type=Path, help="Process a single PDF and exit")
    ap.add_argument("--scan-existing", action="store_true")
    ap.add_argument("--force", action="store_true", help="Regenerate an already completed --once job")
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
    try:
        cfg = load_config(args.config)
    except (ValueError, OSError) as exc:
        LOG.error("Invalid configuration: %s", exc)
        return 2
    if args.force and not args.once:
        ap.error("--force requires --once")
    cfg["force"] = args.force
    for key in ("watch_dir", "templates_dir"):
        if not Path(cfg[key]).is_dir():
            LOG.error("Required input directory is unavailable: %s", cfg[key])
            return 2
    ensure_dirs(
        Path(cfg["output_dir"]),
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

    if Observer is None:
        LOG.error("watchdog is not installed. pip install watchdog  (or use --once)")
        return 1

    work = PdfQueue(
        lambda path: wait_until_stable(path, cfg["watch_settle_seconds"]),
        lambda path, origin, timings: handle_pdf(path, cfg, origin, timings),
        retry_seconds=cfg["retry_seconds"], limit=cfg["watch_queue_limit"],
    )
    handler = PdfHandler(cfg, work)
    observer = _start_observer(cfg, handler)
    if observer is None:
        LOG.error("watchdog is not installed. pip install watchdog  (or use --once)")
        return 1
    work.start()
    if args.scan_existing:
        try:
            scan_existing(cfg, work)
        except OSError:
            LOG.exception("Initial scan interrupted; periodic rescan will retry")
    spawn_model_warmup(cfg)
    last_warm = time.monotonic()
    LOG.info("Watching %s  →  %s", cfg["watch_dir"], cfg["output_dir"])
    exit_code = 0
    try:
        interval = float(cfg.get("watch_rescan_interval") or 0)
        last = time.monotonic()
        backoff = 2.0
        while True:
            time.sleep(1)
            if _watcher_dead(observer):
                # A crash inside the observer or an emitter thread would
                # otherwise leave the process running but deaf to new PDFs
                # (e.g. after an AFP dropout). Recreate the watcher in-place;
                # only give up (and let systemd restart us) if that fails.
                healed = _heal_observer(cfg, handler, observer, backoff)
                if healed is observer:
                    exit_code = 1
                    break
                observer = healed
                backoff = min(backoff * 2, 60.0)
                last = time.monotonic()
                continue
            backoff = 2.0
            if interval and time.monotonic() - last >= interval:
                last = time.monotonic()
                try:
                    scan_existing(cfg, work)
                except Exception:
                    LOG.exception("Periodic rescan failed — watcher keeps running")
            if time.monotonic() - last_warm >= _MODEL_REWARM_S:
                # keep_alive is finite; after a long idle the server would
                # evict the model and the next PDF would pay a cold load.
                last_warm = time.monotonic()
                spawn_model_warmup(cfg)
    except KeyboardInterrupt:
        LOG.info("Interrupted — stopping watcher")
    finally:
        observer.stop()
        work.stop()
    observer.join()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
