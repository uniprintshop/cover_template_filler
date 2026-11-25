"""Shared validation decisions and structured operator-note updates."""

BLOCKING_RISKS = frozenset({
    "multiple_years_unresolved", "author_looks_like_supervisor", "no_university_on_cover",
    "university_not_on_cover", "no_template_for_university", "model_unavailable",
    "model_invalid_response", "university_ambiguous",
})


def can_stamp(fields: dict) -> bool:
    risks = fields.get("risks", [])
    blocked = any(r in BLOCKING_RISKS or r.startswith("university_id_not_in_catalog:") for r in risks)
    return not blocked and all(fields.get(k) for k in ("university_id", "author", "year"))


def set_institution_notes(fields: dict, notes: list[str]) -> None:
    previous = set(fields.get("institution_notes", []))
    fields["notes"] = [note for note in fields.get("notes", []) if note not in previous] + notes
    fields["institution_notes"] = notes
