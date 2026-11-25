"""UPF text-block parsing and encoding, independent of extraction and models."""

import re
from dataclasses import dataclass, field


@dataclass
class GlyphLine:
    align: str
    glyphs: list[tuple[str, str, str, str]]
    plain: str | None = None

    def text(self):
        return self.plain if self.plain is not None else "".join(g[0] for g in self.glyphs)


@dataclass
class TextBlock:
    start: int
    end: int
    raw: str
    kind: str
    lines: list[GlyphLine] = field(default_factory=list)

    def decoded_lines(self):
        return [line.text() for line in self.lines]

    def joined(self):
        return " ".join(t for t in self.decoded_lines() if t.strip())

    def is_empty(self):
        return not re.sub(r"[\x00-\x1f]", "", self.joined()).strip()

    def font_defaults(self):
        for line in self.lines:
            for char, font, size, weight in line.glyphs:
                if char.strip():
                    return line.align, font, size, weight
        return "Center", "Cambria", "20", "Regular"

    def line_font(self, index):
        if 0 <= index < len(self.lines):
            line = self.lines[index]
            for char, font, size, weight in line.glyphs:
                if char.strip():
                    return line.align, font, size, weight
            if line.align:
                _, font, size, weight = self.font_defaults()
                return line.align, font, size, weight
        return self.font_defaults()


def _matching_brace(text, opening):
    # Literal brace glyphs are data rows, not structural delimiters.
    depth = 1
    for match in re.finditer(r"(?m)^[ \t]*([{}])[ \t]*\r?$", text[opening + 1:]):
        depth += 1 if match[1] == "{" else -1
        if depth == 0:
            return opening + 1 + match.start(1)
    return -1


def _detab_lead(line):
    return re.sub(r"^\t+", "", line).rstrip("\t")


def _list_body(block, kind):
    match = re.search(rf"List<{kind}>:(\d+)\s*\{{", block)
    if not match:
        return ""
    opening = block.find("{", match.end() - 1)
    closing = _matching_brace(block, opening)
    if closing < 0:
        raise ValueError("Unclosed UPF text list")
    return block[opening + 1:closing]


def parse_glyph_lines(block):
    body = _list_body(block, "TextLineDetails")
    lines = []
    for match in re.finditer(r"(\d+)\s*\{", body):
        opening = body.find("{", match.end() - 1)
        closing = _matching_brace(body, opening)
        if closing < 0:
            raise ValueError("Unclosed UPF glyph line")
        align, glyphs = "Center", []
        for row in body[opening + 1:closing].splitlines():
            value = _detab_lead(row)
            if value in {"Center", "Left", "Right", "Justify"}:
                align = value
            elif ";" in value:
                parts = value.rsplit(";", 3)
                if len(parts) == 4:
                    glyphs.append(tuple(parts))
        lines.append(GlyphLine(align, glyphs))
    return lines


def parse_string_lines(block):
    body = _list_body(block, "string")
    lines = []
    for match in re.finditer(r"(\d+)\s*\{", body):
        opening = body.find("{", match.end() - 1)
        closing = _matching_brace(body, opening)
        if closing < 0:
            raise ValueError("Unclosed UPF string line")
        values = [_detab_lead(row) for row in body[opening + 1:closing].splitlines() if row.strip("\t")]
        lines.append(GlyphLine("Center", [], values[0] if values else ""))
    return lines


def find_text_blocks(text):
    blocks = []
    for match in re.finditer(r"Object:TextDesignElement\s*\{", text):
        opening = text.find("{", match.end() - 1)
        closing = _matching_brace(text, opening)
        if closing < 0:
            raise ValueError("Unclosed UPF TextDesignElement")
        raw = text[match.start():closing + 1]
        if "List<TextLineDetails>" in raw:
            kind, lines = "glyph", parse_glyph_lines(raw)
        elif "List<string>" in raw:
            kind, lines = "string", parse_string_lines(raw)
        else:
            kind, lines = "unknown", []
        blocks.append(TextBlock(match.start(), closing + 1, raw, kind, lines))
    return blocks


def encode_glyph_line(text, align, font, size, weight, indent):
    rows = [align] + [f"{char};{font};{size};{weight}" for char in text]
    inner = "\n".join(rows)
    return f"{indent}{len(text)}\n{indent}{{\n{inner}\n{indent}}}"


def encode_string_line(text, indent):
    if text.strip() in ("{", "}"):
        raise ValueError("A standalone brace is ambiguous in a UPF string field")
    return f"{indent}{len(text)}\n{indent}{{\n{text}\n{indent}}}"


def replace_list(block, new_lines):
    match = re.search(r"List<(?:TextLineDetails|string)>:\d+\s*\{", block.raw)
    if not match or block.kind not in ("glyph", "string"):
        raise ValueError("TextDesignElement has no supported text list")
    indent = block.raw[block.raw.rfind("\n", 0, match.start()) + 1:match.start()]
    pieces = []
    for index, text in enumerate(new_lines):
        text = re.sub(r"\s+", " ", text)
        if block.kind == "glyph":
            pieces.append(encode_glyph_line(text, *block.line_font(index), indent + "\t"))
        else:
            pieces.append(encode_string_line(text, indent + "\t"))
    opening = block.raw.find("{", match.end() - 1)
    closing = _matching_brace(block.raw, opening)
    kind = "TextLineDetails" if block.kind == "glyph" else "string"
    encoded = "\n".join(pieces)
    replacement = f"List<{kind}>:{len(new_lines)}\n{indent}{{\n{encoded}\n{indent}}}"
    return block.raw[:match.start()] + replacement + block.raw[closing + 1:]
