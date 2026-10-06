"""Generic text-value helpers shared by quiz grading and DZI answer pages.

Standard library only; never ``web.app`` or another application layer. Quiz and
DZI code both import from here so neither depends on the other for a generic
helper.
"""


def unique_text_values(values: list[str]) -> list[str]:
    """Stripped, non-empty values in first-seen order, without duplicates."""
    unique = []
    seen = set()
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        unique.append(text)
        seen.add(text)
    return unique
