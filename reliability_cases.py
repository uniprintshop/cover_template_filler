"""Deterministic, synthetic thesis PDFs with independently specified answers.

The PDF writer uses existing dependencies. Image wordmarks come from repository
templates; they are never accompanied by a hidden text/transcription layer.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import textwrap
import zlib
from copy import deepcopy
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageEnhance, ImageFont
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    DictionaryObject, EncodedStreamObject, DecodedStreamObject, NameObject, NumberObject,
)

SCHEMA = 1
LABELS = {
    "westfälische-hochschule": "Westfälische Hochschule Gelsenkirchen Bocholt Recklinghausen",
    "hspv-nrw": "Hochschule für Polizei und öffentliche Verwaltung Nordrhein-Westfalen",
    "wuppertal": "Bergische Universität Wuppertal",
    "rwth-aachen": "Rheinisch-Westfälische Technische Hochschule Aachen",
    "iu": "IU Internationale Hochschule",
    "rub": "Ruhr-Universität Bochum",
    "bremen": "Universität Bremen",
    "th-koeln": "Technische Hochschule Köln",
}
AUTHOR = "Lina Sommer"
TITLE = "Digitale Zusammenarbeit in verteilten Teams"


def text(value, x=55, y=790, size=13, bold=False):
    return {"text": value, "x": x, "y": y, "size": size, "bold": bold}


def logo(uid, *, box=(55, 712, 440, 100), variant="original", vector=False):
    return {"uid": uid, "box": list(box), "variant": variant, "vector": vector}


def page(uid=None, *, image=False, author=AUTHOR, title=TITLE, year="2026", degree="Masterarbeit",
         author_label=None, include_year=True, extras=(), programme="Digitale Systeme (M.Sc.)",
         language="de", institution_text=None):
    english = language == "en"
    if author_label is None:
        author_label = "Submitted by:" if english else "Vorgelegt von:"
    lines = [text(degree, y=650, size=21, bold=True)]
    title_lines = textwrap.wrap(title, width=48)
    for index, line in enumerate(title_lines):
        lines.append(text(line, y=602 - index * 24, size=17, bold=True))
    programme_y = min(482, 602 - max(0, len(title_lines) - 1) * 24 - 45)
    author_y = min(407, programme_y - 70)
    supervisor_y = min(287, author_y - 120)
    lines += [text(("Programme: " if english else "Studiengang: ") + programme, y=programme_y),
              text(author_label, y=author_y), text(author, y=author_y - 27, size=16),
              text(("Student ID: " if english else "Matrikelnummer: ") + "7418529", y=author_y - 55, size=11),
              text(("First supervisor: " if english else "Erstgutachter: ") + "Prof. Dr. Erik Baumann", y=supervisor_y, size=11),
              text(("Second supervisor: " if english else "Zweitgutachterin: ") + "Prof. Dr. Marie Keller", y=supervisor_y - 21, size=11)]
    if include_year:
        lines.append(text(("Submitted: " if english else "Abgabedatum: ") + f"25.11.{year}", y=min(205, supervisor_y - 82)))
    images = []
    if uid:
        if image:
            images.append(logo(uid))
        else:
            lines[:0] = [text(line, y=790 - index * 23, size=15, bold=True)
                         for index, line in enumerate(textwrap.wrap(institution_text or LABELS.get(uid, uid), 62))]
    lines += list(extras)
    return {"text": lines, "logos": images, "size": [595, 842],
            "institution_id": uid if uid and not image else None,
            "institution_label": (institution_text or LABELS.get(uid, uid)) if uid and not image else None}


def case(case_id, note, *, uid="wuppertal", tags=(), pages=None, author=AUTHOR, title=TITLE,
         year="2026", degree="Masterarbeit", degree_type="master", decision="stamp", tier="core",
         metadata=None, **page_options):
    if degree_type == "dissertation":
        page_options.setdefault("programme", "Promotion zum Dr.-Ing.")
    elif degree_type == "bachelor":
        page_options.setdefault("programme", "Informatik (B.Sc.)")
    return {"id": case_id, "description": note, "tags": list(tags), "tier": tier,
            "pages": pages if pages is not None else [page(uid, author=author, title=title, year=year,
                                                          degree=degree, **page_options)],
            "expected": {"decision": decision, "university_id": uid if decision == "stamp" else None,
                         "fields": {"author": author, "title": title, "year": year,
                                    "degree_type": degree_type, "degree_text": degree}
                         if decision == "stamp" else {}},
            "metadata": metadata or {}}


def specifications():
    """Ground truth is authored here, never inferred from model/OCR output."""
    cases = [
        case("01_digital_german", "Ordinary digital German cover.", uid="th-koeln", tags=("digital",)),
        case("02_english_cover", "English degree wording and a wrapped title.", uid="bremen",
             author="Alex Morgan", title="Reliable Communication Across Distributed Research Teams",
             degree="Bachelor's Thesis", degree_type="bachelor", language="en",
             programme="Applied Computer Science (B.Sc.)", institution_text="University of Bremen", tags=("digital", "english", "degree")),
        case("03_unicode_author", "Umlauts and a hyphenated candidate name.", author="Anna-Lena Weiß",
             tags=("digital", "names")),
        case("04_punctuation_title", "Title with semicolon, umlauts, and an em dash.", uid="rwth-aachen",
             title="Wärme im Quartier; Daten, Modelle und Grenzen — eine Fallstudie", tags=("digital", "title")),
    ]

    # Actual template branding, raster-only; typed fields contain no institution.
    for number, uid, note in (
        (5, "westfälische-hochschule", "School exists only in a page-sized, >2-megapixel raster banner."),
        (6, "hspv-nrw", "Formal school name is present only in the real HSPV image wordmark."),
        (7, "iu", "Real IU wordmark is the only institution evidence."),
        (8, "rwth-aachen", "Real RWTH wordmark is placed in the upper-right corner."),
        (9, "rub", "Real RUB wordmark is at the bottom, not the top of the cover."),
    ):
        sheet = page(uid, image=True)
        if number == 5:
            sheet["logos"][0].update(variant="padded_page", box=[0, 0, 595, 842])
        elif number == 8:
            sheet["logos"][0]["box"] = [305, 712, 235, 100]
        elif number == 9:
            sheet["logos"][0]["box"] = [65, 55, 430, 85]
        cases.append(case(f"{number:02d}_logo_only", note, uid=uid, pages=[sheet],
                          tags=("logo-only", "raster", "real-branding")))

    second = {"text": [text("Ergänzende Angaben zum Deckblatt", y=640)],
              "logos": [logo("wuppertal")], "size": [595, 842]}
    cases.append(case("10_logo_second_page", "Only page two's image identifies the university.",
                      pages=[page(), second], tags=("logo-only", "page-two", "real-branding")))
    traced = page("bremen", image=True)
    traced["logos"][0]["vector"] = True
    cases.append(case("11_vector_wordmark", "Wordmark is outlined vector paths: neither text nor an embedded image.",
                      uid="bremen", pages=[traced], tags=("logo-only", "vector", "ocr-fallback")))
    repeated = page("hspv-nrw", image=True)
    cases.append(case("12_repeated_wordmark", "The identical image is reused on both pages.", uid="hspv-nrw",
                      pages=[repeated, {"text": [text("Eigenständigkeitserklärung")],
                                         "logos": [logo("hspv-nrw")], "size": [595, 842]}],
                      tags=("logo-only", "duplicate-images")))
    for number, variant, uid, note in (
        (13, "small", "hspv-nrw", "A small but readable wordmark, with no digital institution name."),
        (14, "jpeg", "westfälische-hochschule", "Grayscale, compressed image wordmark."),
        (15, "rotated", "bremen", "A 90-degree wordmark along the side of the cover."),
    ):
        sheet = page(uid, image=True)
        sheet["logos"][0]["variant"] = variant
        if variant == "rotated":
            sheet["logos"][0]["box"] = [495, 350, 70, 390]
        cases.append(case(f"{number:02d}_{variant}_wordmark", note, uid=uid, pages=[sheet],
                          tier="stress" if variant == "rotated" else "core", tags=("logo-only", "image-quality")))

    # Symbol-only probes deliberately test a capability beyond wordmark OCR.
    symbol = page("wuppertal", image=True)
    symbol["logos"][0].update(variant="symbol", box=[75, 700, 110, 110])
    cases.append(case("16_symbol_only_probe", "Only the university's text-free graphic mark remains; no OCR-readable name.",
                      pages=[symbol], tier="capability", tags=("logo-only", "symbol-only", "capability-probe")))

    misleading = page("rwth-aachen", extras=[text("Geboren am 12.03.1995 in Wuppertal", y=326, size=11)])
    cases.append(case("17_birthplace_decoy", "Wuppertal is a birthplace; RWTH awards the work.", uid="rwth-aachen",
                      pages=[misleading], tags=("decoy", "birthplace")))
    cases.append(case("18_birth_year", "An old birth year must not replace the submission year.",
                      extras=[text("Geb. am 12.03.1995 in Hagen", y=326, size=11)], tags=("dates", "birthplace")))
    cases.append(case("19_work_start_date", "Work began in 2024; submission is in 2026.",
                      extras=[text("Beginn der Arbeit: 15.10.2024", y=233)], tags=("dates",)))
    address = page(extras=[text("Musterstraße 12", y=154), text("42103 Wuppertal", y=134)])
    cases.append(case("20_address_only_reject", "There is no awarding institution; a postal city is not one.",
                      uid=None, pages=[address], decision="reject", tags=("reject", "address", "decoy")))
    cases.append(case("21_unsupported_school", "An explicitly named university is absent from the catalog.",
                      uid="Nordstadt International University", decision="reject", tags=("reject", "unsupported")))
    no_school = page(extras=[text("Studienzentrum Nord", y=778)])
    cases.append(case("22_missing_school", "Typed cover fields are complete, but no institution appears.",
                      uid=None, pages=[no_school], decision="reject", tags=("reject",)))
    cases.append(case("23_hostile_metadata", "PDF metadata names a different school and supervisor; visible text wins.",
                      metadata={"/Author": "Prof. Dr. Erik Baumann", "/Title": "Technische Hochschule Köln"},
                      tags=("metadata", "decoy")))
    standalone = page("wuppertal", author_label="")
    cases.append(case("24_standalone_author", "Candidate stands alone; two titled supervisors appear below.",
                      pages=[standalone], tags=("names", "supervisors")))
    cases.append(case("25_surname_first", "Surname-first author with comma and umlaut.", author="Öztürk, Cem",
                      tags=("names",)))
    cases.append(case("26_name_particles", "Lowercase surname particles must survive.", author="Anna-Lena von der Meer",
                      tags=("names",)))
    bilingual = page("wuppertal", author="Nora Seidel, Hagen", author_label="Autorennamen, Geburtsort // Name, Place of Birth")
    cases.append(case("27_name_and_birthplace", "A bilingual form puts birthplace after the name; stamp only the person.",
                      pages=[bilingual], author="Nora Seidel", tags=("names", "bilingual", "birthplace"), tier="stress"))
    joint = page("wuppertal", author="Lina Sommer und Emil Berger")
    cases.append(case("28_joint_authors", "A joint thesis names two submitting candidates.", pages=[joint],
                      author="Lina Sommer und Emil Berger", tags=("names", "joint-work"), tier="stress"))
    no_year = page("wuppertal", include_year=False)
    cases.append(case("29_missing_year_default", "No printed year; the suite fixes publish_year to 2026 and expects a flagged fallback.",
                      pages=[no_year], tags=("dates", "fallback")))
    cases[-1]["expected"]["required_risks"] = ["year_from_publish_year_default"]
    generic = page("wuppertal", degree="Wissenschaftliche Abschlussarbeit")
    cases.append(case("30_generic_degree", "Generic thesis wording with a concrete M.Sc. programme.", pages=[generic],
                      degree="Master-Thesis", tags=("degree",)))
    cases.append(case("31_dissertation", "A doctoral thesis, not a master's programme.", degree="Dissertation",
                      degree_type="dissertation", tags=("degree",)))
    cases.append(case("32_no_title", "No thesis title is printed; title variants must be skipped.", title="",
                      tags=("title", "missing-field")))

    contents = {"size": [595, 842], "logos": [], "text": [text("Inhaltsverzeichnis", bold=True)] + [
        text(line, y=745 - i * 24, size=11) for i, line in enumerate([
            "1 Einleitung ............................................. 1",
            "2 Literatur zur Ruhr-Universität Bochum .................... 8",
            "3 Vergleich mit der Universität Bremen ................... 17",
            "4 Ergebnisse ............................................ 32",
        ])]}
    cases.append(case("33_contents_decoys", "Page-two contents mention other universities; the cover's institution awards the work.",
                      pages=[page("wuppertal"), contents], tags=("page-two", "decoy", "contents")))
    third = page("bremen")
    cases.append(case("34_school_on_page_three", "Institution occurs only on page three, beyond the supported evidence window.",
                      uid=None, pages=[page(), deepcopy(contents), third], decision="reject", tags=("reject", "page-three")))
    # Remove school mentions in the intervening contents page for the boundary case.
    cases[-1]["pages"][1]["text"] = [text("Eigenständigkeitserklärung"), text("Hiermit erkläre ich die selbstständige Arbeit.", y=745)]

    ambiguous = page()
    ambiguous["logos"] = [logo("bremen", box=(55, 720, 220, 90)), logo("wuppertal", box=(315, 720, 225, 90))]
    ambiguous["text"].append(text("Gemeinsame Abschlussarbeit beider Hochschulen", y=686, size=12))
    cases.append(case("35_ambiguous_awarders", "Two equal awarding bodies and no single approved joint foil: require operator review.",
                      uid=None, pages=[ambiguous], decision="reject", tags=("reject", "multiple-logos", "ambiguity"), tier="stress"))
    partner = page()
    partner["logos"] = [logo("westfälische-hochschule", box=(55, 730, 390, 90)),
                        logo("hspv-nrw", box=(55, 65, 270, 75))]
    partner["text"] += [text("Abschlussverleihende Hochschule: oberes Logo", y=691, size=11),
                        text("Unteres Logo: Kooperationspartner, kein Abschlussverleiher", y=151, size=10)]
    cases.append(case("36_awarder_and_partner", "Only image branding distinguishes the awarding school from a named-role partner.",
                      uid="westfälische-hochschule", pages=[partner], tier="stress", tags=("logo-only", "multiple-logos", "partner")))
    cases.append(case("37_full_page_scan", "Flattened full-page scan; no selectable text survives.",
                      pages=[{**page("bremen"), "scan": {"dpi": 170, "angle": 0}}], uid="bremen",
                      tags=("scan", "ocr")))
    cases.append(case("38_skewed_scan", "Grayscale JPEG scan with a mild 1.6-degree skew.",
                      pages=[{**page("th-koeln"), "scan": {"dpi": 170, "angle": 1.6, "jpeg": True}}],
                      uid="th-koeln", tier="stress", tags=("scan", "ocr", "image-quality")))
    landscape = page("iu", image=True)
    landscape["size"] = [842, 595]
    cases.append(case("39_landscape_cover", "Landscape cover with a raster-only institution wordmark.", uid="iu",
                      pages=[landscape], tags=("logo-only", "landscape")))
    crowded = page("westfälische-hochschule", image=True)
    crowded["decorations"] = 14
    cases.append(case("40_many_images", "Decorative images precede the real wordmark and exercise the embedded-image budget.",
                      uid="westfälische-hochschule", pages=[crowded], tier="stress", tags=("logo-only", "image-budget")))
    long_title = ("Digitale Zusammenarbeit in verteilten Teams unter wechselnden organisatorischen Bedingungen: "
                  "Eine qualitative Untersuchung zu Kommunikation, Verantwortung und Vertrauen in kleinen "
                  "Forschungsgruppen sowie zu den Grenzen automatisierter Auswertungsverfahren")
    cases.append(case("41_long_wrapped_title", "A long, multi-line title must remain complete and verbatim.",
                      title=long_title, tags=("title", "long-text"), tier="stress"))
    blank = {"size": [595, 842], "logos": [], "text": [text("Deckblatt folgt auf der nächsten Seite", y=400)]}
    cases.append(case("42_blank_then_cover", "A sparse separator page precedes the actual cover on page two.",
                      pages=[blank, page("hspv-nrw", image=True)], uid="hspv-nrw", tags=("page-two", "logo-only")))
    table = page("rwth-aachen")
    table["text"] = [line for line in table["text"] if not 330 <= line["y"] <= 420]
    table["text"] += [text("Name, Vorname:", y=400), text("Weiß, Anna-Lena", x=280, y=400),
                       text("Geburtsdatum:", y=370), text("12.03.1994", x=280, y=370)]
    table["rules"] = [[50, 345, 495, 80], [265, 345, 0, 80], [50, 385, 495, 0]]
    cases.append(case("43_table_form", "Two-column form with surname-first author and a separate birth date.",
                      uid="rwth-aachen", pages=[table], author="Weiß, Anna-Lena", tags=("table", "names", "dates")))
    split = page("westfälische-hochschule", image=True)
    split["text"] = [line for line in split["text"] if not 330 <= line["y"] <= 420]
    author_page = {"text": [text("Angaben zur Verfasserin", bold=True), text("Vorgelegt von:", y=700),
                            text(AUTHOR, y=674, size=16), text("Matrikelnummer: 7418529", y=646)],
                   "logos": [], "size": [595, 842]}
    cases.append(case("44_author_on_second_page", "Logo/title are on page one; the candidate's details only appear on page two.",
                      uid="westfälische-hochschule", pages=[split, author_page], tags=("logo-only", "page-two", "split-fields")))
    rotated = page("iu", image=True)
    rotated["rotation"] = 90
    cases.append(case("45_rotated_pdf_page", "A PDF /Rotate flag changes page orientation without flattening its text.",
                      uid="iu", pages=[rotated], tags=("logo-only", "rotation")))
    footer = {"size": [595, 842], "logos": [], "institution_id": "wuppertal", "institution_label": LABELS["wuppertal"],
              "text": [text("Zusammenfassung", bold=True)] + [
                  text("Diese Untersuchung betrachtet Zusammenarbeit, Vertrauen und digitale Kommunikation in Teams.",
                       y=760 - index * 14, size=8) for index in range(40)
              ] + [text(LABELS["wuppertal"], y=115, size=12)]}
    cases.append(case("46_institution_after_abstract", "The only institutional name is a page-two footer after a long abstract.",
                      pages=[page(), footer], tier="stress", tags=("page-two", "long-text", "prompt-budget")))
    return cases


def _flatten(image):
    rgba = image.convert("RGBA")
    white = Image.new("RGBA", image.size, "white")
    white.alpha_composite(rgba)
    return white.convert("RGB")


def load_logos(templates_dir: Path, universities: list[dict]):
    images, provenance = {}, {}
    for uid in LABELS:
        uni = next(u for u in universities if u["id"] == uid)
        path = templates_dir / uni["template"]
        encoded = re.findall(r"Object:Image:\d+\s*\{\s*([A-Za-z0-9+/=\s]+?)\s*\}", path.read_text())
        decoded = []
        for value in encoded:
            raw = base64.b64decode(value)
            with Image.open(io.BytesIO(raw)) as image:
                decoded.append((_flatten(image), raw))
        if not decoded:
            raise ValueError(f"No raster branding in {path}")
        image, raw = max(decoded, key=lambda pair: pair[0].width * pair[0].height)
        images[uid] = image
        provenance[uid] = {"template": path.name, "sha256": hashlib.sha256(raw).hexdigest(),
                           "size": list(image.size)}
    return images, provenance


def transformed(image, variant):
    image = image.copy()
    if variant == "padded_page":
        canvas = Image.new("RGB", (1241, 1754), "white")
        image.thumbnail((1100, 340), Image.Resampling.LANCZOS)
        canvas.paste(image, (65, 60))
        image = canvas
    elif variant == "small":
        image.thumbnail((620, 230), Image.Resampling.LANCZOS)
    elif variant == "jpeg":
        image.thumbnail((650, 240), Image.Resampling.LANCZOS)
        image = ImageEnhance.Contrast(image.convert("L")).enhance(.8).convert("RGB")
        stream = io.BytesIO()
        image.save(stream, "JPEG", quality=35)
        image = Image.open(io.BytesIO(stream.getvalue())).convert("RGB")
    elif variant == "rotated":
        image = image.rotate(90, expand=True, fillcolor="white")
    elif variant == "symbol":
        # The repository's Wuppertal logo has its geometric sign at the left.
        # A saved preview and an OCR-free preflight make this crop auditable.
        image = image.crop((0, 0, int(image.width * .36), image.height))
    return image


class PdfCanvas:
    def __init__(self):
        self.writer = PdfWriter()
        self.images = {}
        self.fonts = DictionaryObject()
        for name, family in (("F1", "Helvetica"), ("F2", "Helvetica-Bold")):
            font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                     NameObject("/Subtype"): NameObject("/Type1"),
                                     NameObject("/BaseFont"): NameObject("/" + family),
                                     NameObject("/Encoding"): NameObject("/WinAnsiEncoding")})
            self.fonts[NameObject("/" + name)] = self.writer._add_object(font)

    def add_image(self, resources, image, box):
        raw = image.convert("RGB").tobytes()
        digest = hashlib.sha256(raw + str(image.size).encode()).hexdigest()
        if digest not in self.images:
            stream = EncodedStreamObject()
            stream._data = zlib.compress(raw)
            stream.update({NameObject("/Type"): NameObject("/XObject"),
                           NameObject("/Subtype"): NameObject("/Image"),
                           NameObject("/Width"): NumberObject(image.width),
                           NameObject("/Height"): NumberObject(image.height),
                           NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                           NameObject("/BitsPerComponent"): NumberObject(8),
                           NameObject("/Filter"): NameObject("/FlateDecode")})
            self.images[digest] = self.writer._add_object(stream)
        key = NameObject(f"/I{len(resources) + 1}")
        resources[key] = self.images[digest]
        x, y, width, height = fit_box(image, box)
        return f"q {width:.3f} 0 0 {height:.3f} {x:.3f} {y:.3f} cm {key} Do Q"

    def add_page(self, spec, logos):
        width, height = spec.get("size", [595, 842])
        sheet = self.writer.add_blank_page(width=width, height=height)
        resources = DictionaryObject()
        commands = [f"q {width / 595:.5f} 0 0 {height / 842:.5f} 0 0 cm"]
        for index in range(spec.get("decorations", 0)):
            icon = Image.new("RGB", (100, 45), (230, 235, 240))
            draw = ImageDraw.Draw(icon)
            draw.ellipse((5 + index % 15, 5, 35 + index % 15, 35), fill=(40 + index * 9, 80, 120))
            commands.append(self.add_image(resources, icon, (48 + index * 35, 22, 28, 15)))
        for item in spec.get("logos", []):
            image = transformed(logos[item["uid"]], item.get("variant", "original"))
            commands.append(vector_commands(image, item["box"]) if item.get("vector")
                            else self.add_image(resources, image, item["box"]))
        for item in spec.get("text", []):
            encoded = item["text"].encode("cp1252").hex()
            font = "F2" if item.get("bold") else "F1"
            commands.append(f"BT /{font} {item['size']} Tf 1 0 0 1 {item['x']} {item['y']} Tm <{encoded}> Tj ET")
        for x, y, rect_width, rect_height in spec.get("rules", []):
            commands.append(f"q .5 w {x} {y} {rect_width} {rect_height} re S Q")
        commands += ["BT /F1 7 Tf 1 0 0 1 55 10 Tm (SYNTHETIC RELIABILITY FIXTURE) Tj ET", "Q"]
        sheet[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): self.fonts,
                                                           NameObject("/XObject"): resources})
        stream = DecodedStreamObject()
        stream.set_data("\n".join(commands).encode("ascii"))
        sheet[NameObject("/Contents")] = self.writer._add_object(stream.flate_encode())
        if spec.get("rotation"):
            sheet.rotate(spec["rotation"])

    def bytes(self, metadata=None):
        self.writer.add_metadata({"/Author": "Synthetic fixture generator", "/Title": "Synthetic thesis cover",
                                  "/CreationDate": "D:20261125120000+00'00'", **(metadata or {})})
        stream = io.BytesIO()
        self.writer.write(stream)
        return stream.getvalue()


def fit_box(image, box):
    x, y, width, height = box
    factor = min(width / image.width, height / image.height)
    actual_width, actual_height = image.width * factor, image.height * factor
    return x + (width - actual_width) / 2, y + (height - actual_height) / 2, actual_width, actual_height


def vector_commands(image, box):
    image = image.copy()
    image.thumbnail((800, 300), Image.Resampling.LANCZOS)
    mask = image.convert("L").point(lambda value: 0 if value < 170 else 255)
    x, y, width, height = fit_box(image, box)
    sx, sy = width / image.width, height / image.height
    commands = ["q 0 g"]
    pixels = mask.load()
    for row in range(image.height):
        start = None
        for column in range(image.width + 1):
            dark = column < image.width and pixels[column, row] == 0
            if dark and start is None:
                start = column
            elif not dark and start is not None:
                commands.append(f"{x + start * sx:.3f} {y + (image.height - row - 1) * sy:.3f} "
                                f"{(column - start) * sx:.3f} {sy:.3f} re f")
                start = None
    return "\n".join(commands + ["Q"])


def render_case(spec, logos):
    canvas = PdfCanvas()
    for sheet in spec["pages"]:
        if "scan" not in sheet:
            canvas.add_page(sheet, logos)
            continue
        clean = PdfCanvas()
        clean.add_page(sheet, logos)
        with pdfium.PdfDocument(clean.bytes()) as document:
            pdf_page = document[0]
            bitmap = pdf_page.render(scale=sheet["scan"]["dpi"] / 72)
            image = bitmap.to_pil().copy().convert("RGB")
            bitmap.close()
            pdf_page.close()
        angle = sheet["scan"].get("angle", 0)
        if angle:
            image = image.rotate(angle, resample=Image.Resampling.BICUBIC, expand=False, fillcolor="white")
        if sheet["scan"].get("jpeg"):
            stream = io.BytesIO()
            image.convert("L").save(stream, "JPEG", quality=55)
            image = Image.open(io.BytesIO(stream.getvalue())).convert("RGB")
        # A full-page image contains everything; no hidden text or footer is added.
        scanned = canvas.writer.add_blank_page(width=595, height=842)
        resources = DictionaryObject()
        command = canvas.add_image(resources, image, [0, 0, 595, 842])
        scanned[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"): resources})
        content = DecodedStreamObject()
        content.set_data(command.encode("ascii"))
        scanned[NameObject("/Contents")] = canvas.writer._add_object(content)
    return canvas.bytes(spec.get("metadata"))


def build_corpus(output: Path, templates_dir: Path, universities: list[dict], *, overwrite=False, cases=None):
    output, templates_dir = output.resolve(), templates_dir.resolve()
    if output.is_relative_to(templates_dir) or templates_dir.is_relative_to(output):
        raise ValueError("Generated corpus must not overlap the template source")
    marker = output / ".synthetic-corpus"
    if output.exists() and any(output.iterdir()):
        if not overwrite or not marker.is_file():
            raise FileExistsError(f"Refusing to replace {output}; use --overwrite only for an existing generated corpus")
    output.mkdir(parents=True, exist_ok=True)
    marker.write_text("Generated by reliability_cases.py\n")
    (output / "assets").mkdir(exist_ok=True)
    (output / "pdfs").mkdir(exist_ok=True)
    (output / "previews").mkdir(exist_ok=True)
    logos, provenance = load_logos(templates_dir, universities)
    for uid, image in logos.items():
        image.save(output / "assets" / f"{uid}.png")
    transformed(logos["wuppertal"], "symbol").save(output / "assets" / "symbol-only-probe.png")
    manifest = {"schema": SCHEMA, "synthetic": True, "publish_year": "2026",
                "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "templates_dir": str(templates_dir.resolve()), "assets": provenance, "cases": []}
    for spec in specifications() if cases is None else cases:
        raw = render_case(spec, logos)
        filename = "pdfs/" + spec["id"] + (".PdF" if spec["id"].startswith("39_") else ".pdf")
        (output / filename).write_bytes(raw)
        with pdfium.PdfDocument(raw) as document:
            first = document[0]
            bitmap = first.render(scale=1)
            preview = bitmap.to_pil().copy().convert("RGB")
            preview.save(output / "previews" / f"{spec['id']}.png")
            bitmap.close()
            first.close()
        digital = "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(raw)).pages[:2])
        entry = {**spec, "pdf": filename, "sha256": hashlib.sha256(raw).hexdigest(),
                 "digital_text_sha256": hashlib.sha256(digital.encode()).hexdigest(),
                 "page_count": len(spec["pages"])}
        manifest["cases"].append(entry)
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    columns, cell_w, cell_h = 6, 210, 320
    contact = Image.new("RGB", (columns * cell_w, ((len(manifest["cases"]) + columns - 1) // columns) * cell_h), "#dce1e6")
    draw = ImageDraw.Draw(contact)
    font = ImageFont.load_default(size=12)
    for index, entry in enumerate(manifest["cases"]):
        with Image.open(output / "previews" / f"{entry['id']}.png") as image:
            image.thumbnail((cell_w - 12, cell_h - 40))
            x, y = index % columns * cell_w, index // columns * cell_h
            contact.paste(image, (x + 6, y + 28))
            draw.text((x + 6, y + 5), entry["id"][:27], font=font, fill="black")
    contact.save(output / "contact_sheet.png")
    return manifest
