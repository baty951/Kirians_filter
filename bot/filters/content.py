import re
import unicodedata
from collections.abc import Iterable
from functools import lru_cache
from itertools import groupby
from urllib.parse import urlsplit

from aiogram.types import MessageEntity

# Detects any http(s):// or t.me / telegram link. Bare domains come as `url`
# entities from Telegram, so they are not guessed here ("ok.thanks" is not a link).
_LINK_RE = re.compile(r"\bhttps?://\S+|\bwww\.\S+|\b(?:t|telegram)\.me/\S+", re.IGNORECASE)
_MENTION_RE = re.compile(r"(?<![\w@])@[A-Za-z][A-Za-z0-9_]{3,31}")
_URL_TRAILING = ".,;:!?)]}>»\"'"
_SCHEME_RE = re.compile(r"[a-z][a-z0-9+.-]*:(?!\d)", re.IGNORECASE)
_DOMAIN_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")

# Zero-width / invisible / filler characters used to split words.
_INVISIBLE = re.compile(
    "[­͏؜ᅟᅠ឴឵᠋-᠏​-‏"
    "‪-‮⁠-⁯ㅤ︀-️﻿]"
)
# Look-alike letters only (n/п, u/и, digits and Greek are left out deliberately).
# Shipped 2-letter stems that would otherwise prefix-match common words ("cu" -> "customer").
# Not "ёб"/"oç": those are real prefixes ("ебнутый"), so a blanket length rule would lose them.
_WHOLE_WORD_STEMS = frozenset({"cu", "كس", "زب"})
_LAT2CYR = str.maketrans("abcehkmoptxy", "авсенкмортху")
_CYR2LAT = str.maketrans("авсенкмортху", "abcehkmoptxy")
_LATIN, _CYRILLIC = re.compile("[a-z]"), re.compile("[а-яё]")


@lru_cache(maxsize=None)
def _expands(char: str) -> bool:
    # NFKD (not NFKC) bounds the length before composition.
    return len(unicodedata.normalize("NFKD", char)) > 4


def fold(text: str) -> str:
    """Drop invisible chars, NFKC, casefold, ё -> е (match-time only).

    Characters that expand to more than 4 chars (U+FDFA...) become a space, so
    the output is at most 4x the input; ligatures like "ﬃ" are kept.
    """
    if not text.isascii():
        text = "".join(" " if _expands(c) else c for c in _INVISIBLE.sub("", text))
    text = unicodedata.normalize("NFKC", text)  # may turn U+3164 into U+1160 etc.
    return _INVISIBLE.sub("", text).casefold().replace("ё", "е")


def _convert(text: str, table: dict[int, int], leftover: re.Pattern[str]) -> str:
    """Convert mixed Latin/Cyrillic tokens that are fully convertible; other tokens stay as is."""
    def token(match: re.Match[str]) -> str:
        word = match.group()
        if not (_LATIN.search(word) and _CYRILLIC.search(word)):
            return word
        converted = word.translate(table)
        return word if leftover.search(converted) else converted
    return re.sub(r"\w+", token, text)


def homoglyph_variants(text: str) -> tuple[str, str]:
    """(to_cyr, to_lat) variants of already folded text. All-Latin or all-Cyrillic tokens are never changed."""
    return _convert(text, _LAT2CYR, _LATIN), _convert(text, _CYR2LAT, _CYRILLIC)


@lru_cache(maxsize=4096)
def _compile_word(pattern: str, is_regex: bool) -> re.Pattern[str] | None:
    """Compile a banned word into a matcher (cached across messages).

    Plain words are anchored with a leading word boundary so the pattern must
    begin a word: stem "ass" no longer matches inside "class"/"pass", while
    suffix inflections still match ("fuck" -> "fucking"). Every letter run is
    made repeat-tolerant ("шлюх" -> "ш+л+ю+х+", "coon" -> "c+o{2,}n+"), so
    "шлюхааа" is caught without widening "coon" to "content". Regex patterns
    are used as-is (anchor them yourself if needed). Returns None on an invalid pattern, or one that would match every message.
    """
    try:
        if is_regex:
            compiled = re.compile(pattern, re.IGNORECASE)
            # A pattern that matches "" would hit every message.
            return None if compiled.search("") else compiled
        folded = fold(pattern)
        if not folded.strip():  # only invisible chars/spaces: a bare \b would hit every word
            return None
        runs = ((c, len(list(g))) for c, g in groupby(folded))
        expr = r"\b" + "".join(re.escape(c) + ("+" if n == 1 else f"{{{n},}}") for c, n in runs)
        if folded in _WHOLE_WORD_STEMS:
            expr += r"\b"
        return re.compile(expr, re.IGNORECASE)
    except re.error:
        return None


def find_banned_word(text: str, words: Iterable[tuple[str, bool]]) -> str | None:
    """Return the matched pattern if the text hits a banned word, else None.

    Plain words run on the folded text and its homoglyph variants; admin
    regexes run on the original text plus the same variants.
    """
    text = text[:4096]
    base = fold(text)  # at most 4x the (already trimmed) input
    variants = (base, *homoglyph_variants(base))
    plain = "\n".join(dict.fromkeys(variants))  # a newline never matches a phrase's space; \b works at it
    regex = tuple(dict.fromkeys((text, *variants)))
    # ponytail: merge into one alternation regex per chat if profiling shows a problem
    for pattern, is_regex in words:
        compiled = _compile_word(pattern, is_regex)
        if compiled is None:
            continue
        if any(compiled.search(v) for v in regex) if is_regex else compiled.search(plain):
            return pattern
    return None


def _host(url: str) -> str:
    """Punycode host of a URL-ish string; "" if it is not an http(s) host."""
    url = url.strip().replace("\\", "/").rstrip(_URL_TRAILING)  # browsers treat \ as / in http(s)
    if "://" not in url:
        if _SCHEME_RE.match(url):
            return ""  # mailto:, tg:... etc.
        url = "http://" + url
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https"):
            return ""
        host = (parts.hostname or "").rstrip(".")
        return host.encode("idna").decode() if host else ""
    except (ValueError, UnicodeError):
        return ""


def _mask_text_links(text: str, entities: Iterable[MessageEntity]) -> str:
    """Blank the visible label of text_link entities (UTF-16 offsets)."""
    data = bytearray(text.encode("utf-16-le", "surrogatepass"))
    for entity in entities:
        if entity.type == "text_link" and (entity.offset + entity.length) * 2 <= len(data):
            data[entity.offset * 2:(entity.offset + entity.length) * 2] = b" \x00" * entity.length
    return data.decode("utf-16-le", "replace")


def link_hosts(text: str, entities: Iterable[MessageEntity] = ()) -> set[str]:
    entities = tuple(entities)
    hosts = set()
    for entity in entities:
        if entity.type == "url":
            hosts.add(_host(entity.extract_from(text)))
        elif entity.type == "text_link" and entity.url:
            hosts.add(_host(entity.url))  # the real target, not the visible text
    # The label of a text_link is not a link of its own; its real target was checked above.
    hosts.update(_host(match.group()) for match in _LINK_RE.finditer(_mask_text_links(text, entities)))
    return hosts


def contains_link(text: str, entities: Iterable[MessageEntity] = (), allowed: Iterable[str] = ()) -> bool:
    """True if any link host is outside the allowed domains (and their subdomains)."""
    allowed = tuple(allowed)
    return any(
        not any(host == d or host.endswith("." + d) for d in allowed)
        for host in link_hosts(text, entities)
    )


def contains_mention(text: str, entities: Iterable[MessageEntity] = ()) -> bool:
    return any(e.type == "mention" for e in entities) or bool(_MENTION_RE.search(text))


def normalize_domain(raw: str) -> str | None:
    host = _host(raw.strip())
    if host.startswith("www."):
        host = host[4:]
    return host if len(host) <= 100 and _DOMAIN_RE.match(host) else None


def parse_domains(value: str | None) -> list[str]:
    """Parse a comma-separated stored value into domains."""
    return [p for p in (part.strip().lower() for part in (value or "").split(",")) if p]
