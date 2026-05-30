import re

# Named writing systems -> Unicode character ranges. Used to block messages
# written in scripts foreign to a chat (a common spam vector).
# Ranges use \uXXXX escapes; the `re` engine interprets them inside the class.
SCRIPT_RANGES: dict[str, str] = {
    "arabic": r"\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufeff",
    "cyrillic": r"\u0400-\u052f",
    "chinese": r"\u3400-\u4dbf\u4e00-\u9fff",
    "hebrew": r"\u0590-\u05ff",
    "greek": r"\u0370-\u03ff",
    "korean": r"\u1100-\u11ff\u3130-\u318f\uac00-\ud7af",
    "japanese": r"\u3040-\u30ff",
    "devanagari": r"\u0900-\u097f",
    "thai": r"\u0e00-\u0e7f",
}

# A message must contain at least this many chars of a blocked script to trip
# the filter -- avoids false positives from a single stray symbol.
MIN_BLOCKED_CHARS = 2

_COMPILED: dict[str, re.Pattern[str]] = {
    name: re.compile(f"[{ranges}]") for name, ranges in SCRIPT_RANGES.items()
}


def parse_scripts(value: str | None) -> list[str]:
    """Parse a comma-separated stored value into known script names."""
    if not value:
        return []
    return [s for s in (p.strip().lower() for p in value.split(",")) if s in SCRIPT_RANGES]


def normalize_scripts(names: list[str]) -> tuple[list[str], list[str]]:
    """Split user-supplied names into (valid, unknown)."""
    valid, unknown = [], []
    for raw in names:
        name = raw.strip().lower()
        if not name:
            continue
        (valid if name in SCRIPT_RANGES else unknown).append(name)
    return valid, unknown


def detect_blocked_script(text: str, scripts: list[str]) -> str | None:
    """Return the first blocked script present in `text`, or None."""
    for name in scripts:
        pattern = _COMPILED.get(name)
        if pattern and len(pattern.findall(text)) >= MIN_BLOCKED_CHARS:
            return name
    return None
