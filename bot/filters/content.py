import re
from functools import lru_cache

from bot.database.models import BannedWord

# Detects any http(s):// or t.me / telegram link, and bare domains.
_LINK_RE = re.compile(
    r"(https?://|www\.|t\.me/|telegram\.me/|@[\w]{4,})|"
    r"\b[\w-]+\.(com|net|org|ru|io|me|info|xyz|top|link)\b",
    re.IGNORECASE,
)


@lru_cache(maxsize=4096)
def _compile_word(pattern: str, is_regex: bool) -> re.Pattern[str] | None:
    """Compile a banned word into a matcher (cached across messages).

    Plain words are anchored with a leading word boundary so the pattern must
    begin a word: stem "ass" no longer matches inside "class"/"pass", while
    suffix inflections still match ("fuck" -> "fucking"). The boundary is
    Unicode-aware, so it works for Cyrillic/Arabic too. Regex patterns are used
    as-is (anchor them yourself if needed). Returns None on invalid regex.
    """
    try:
        expr = pattern if is_regex else r"\b" + re.escape(pattern)
        return re.compile(expr, re.IGNORECASE)
    except re.error:
        return None


def find_banned_word(text: str, words: list[BannedWord]) -> str | None:
    """Return the matched pattern if the text hits a banned word, else None."""
    for word in words:
        regex = _compile_word(word.pattern, word.is_regex)
        if regex and regex.search(text):
            return word.pattern
    return None


def contains_link(text: str) -> bool:
    return bool(_LINK_RE.search(text))
