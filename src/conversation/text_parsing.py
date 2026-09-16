"""Deterministic ("fixed") parsing of free text into the value shape
validate_answer / the choice-matching step in service.py already expect --
slotted in BEFORE either of those, never instead of them. See
docs/plans/text-parsing-pipeline.md for the full design and the diagram
this implements.

Scope: INT, FLOAT, DATE, DATETIME (this module's two number/date functions)
and OPTION/BOOLEAN (match_choice). VARCHAR and GEODATA never reach this
module -- service.py only calls in for a validation_rule["type"] in
FIXED_PARSE_TYPES, or for real form-question choice matching.

Deliberately narrow, per team decision: most farmers type digits or an
ISO-ish date directly, and those already-well-formed answers must keep
working completely unchanged -- see each function's "already valid" fast
path, checked first. Word-based Thai input (spelled-out numbers, "วันนี้"/
"พรุ่งนี้") is the actual fallback this module adds, entirely via existing
libraries (pythainlp, dateparser) -- nothing here hand-rolls Thai number-word
or date grammar.

Whatever a library can't confidently handle is NOT guessed at further: the
functions below return None, and the caller (service.py's handle_answer)
passes the farmer's ORIGINAL text through to validate_answer unchanged in
that case -- exactly as if this module didn't exist. That, not a separate
"couldn't understand" message, is what puts the farmer back in front of the
field's own configured error_message (e.g. "กรุณากรอกจำนวนพัดลมเป็นจำนวนเต็ม
0-50"), the same one they'd see for any other invalid answer to that field --
one message per field, not a different one depending on why parsing failed.
"""

from datetime import datetime
from typing import TYPE_CHECKING

import dateparser
from pythainlp.util import thaiword_to_num

if TYPE_CHECKING:
    from src.conversation.service import Choice

# validation_rule["type"] values this module knows how to pre-parse --
# service.py checks membership here before calling try_fixed_parse at all.
# OPTION/BOOLEAN are handled by match_choice below instead, a separate path
# (those questions carry no validation_rule -- see validation.py's docstring).
FIXED_PARSE_TYPES = frozenset({"INT", "FLOAT", "DATE", "DATETIME"})

# Trailing particles/politeness words a farmer might add without changing
# the actual choice being picked ("ใช่ครับ" still means "ใช่") -- stripped
# before re-trying an exact match. Longest-first so "นะครับ" doesn't leave a
# dangling "ครับ" half-stripped on the first pass.
#
# Deliberately NOT general fuzzy/similarity matching (e.g. difflib): choice
# labels are often short and near-identical except for one distinguishing
# word or number (this repo's own test fixtures use "farm 0".."farm 24" for
# a real OPTION field) -- a similarity-ratio match would confidently resolve
# to the WRONG neighbor instead of admitting it doesn't know. Caught live in
# this module's own test suite: "farm 15" scores 0.92 similarity against
# "farm 1", nowhere near a safe-looking cutoff, and is a real answer on a
# DIFFERENT page than the one being shown -- silently accepting it would
# have stored the wrong farm. Particle-stripping can't cause that kind of
# collision: it only ever shortens text by a known trailing word, never
# turns one distinct label into another.
_TRAILING_PARTICLES = (
    "นะครับ",
    "นะคะ",
    "ครับผม",
    "ครับ",
    "ค่ะ",
    "คะ",
    "จ้า",
    "จ๊ะ",
    "นะ",
    "เลย",
)

# A Thai Buddhist Era year is always ~543 years ahead of its Gregorian
# equivalent, by definition -- so a genuine BE year for a near-term farm-log
# date lands within a few years of (this Gregorian year + 543), a narrow and
# specific band. Deliberately NOT "any year further out than a few years
# from now": that would also swallow an ordinary, deliberately-far-future
# Gregorian date (caught live in this module's own test suite -- a fixed
# test date of 2099, used elsewhere to exercise "reject a future date,"
# is nowhere near 2569 and must be left alone, not reinterpreted as BE and
# shifted to 1556).
_BE_OFFSET = 543
_BE_YEAR_SLACK = 5


def parse_number(raw_text: str) -> str | None:
    """Returns a numeric string ready for validate_answer's INT/FLOAT
    validators (which parse it themselves), or None if neither the direct
    parse nor the Thai-numeral fallback could make sense of it.
    """
    text = raw_text.strip().replace(",", "")
    if not text:
        return None

    # Already a plain number -- Python's int()/float() parse Thai digit
    # characters (๐-๙) natively, same as ASCII ones, so this already covers
    # both without any translation table. The overwhelmingly common case,
    # and must keep behaving byte-for-byte as it did before this module
    # existed.
    try:
        int(text)
        return text
    except ValueError:
        pass
    try:
        float(text)
        return text
    except ValueError:
        pass

    # Word-spelled Thai numerals ("เก้าร้อย", "เก้าร้อยห้าสิบเอ็ด") -- the
    # actual fallback. thaiword_to_num raises ValueError on anything it
    # can't parse (garbage text, a number word with a trailing unit like
    # "เก้าร้อยกิโล", internal spaces) -- deliberately not caught any
    # harder than that; per team decision, this stays a narrow backup for a
    # bare spelled-out number, not a general text-to-number extractor.
    try:
        return str(thaiword_to_num(text))
    except ValueError:
        return None


def parse_date(raw_text: str, *, is_datetime: bool = False) -> str | None:
    """Returns an ISO date (or datetime, if is_datetime) string ready for
    validate_answer's DATE/DATETIME validators, or None if dateparser
    couldn't resolve it at all.
    """
    text = raw_text.strip()
    if not text:
        return None

    parsed = dateparser.parse(text, languages=["th"])
    if parsed is None:
        return None

    # Buddhist Era correction -- see _BE_OFFSET's docstring above. Only
    # fires when the year is within a few years of "this Gregorian year +
    # 543" specifically, not merely "far in the future" -- an ordinary,
    # deliberately-far-future Gregorian date is never touched.
    expected_be_year = datetime.now().year + _BE_OFFSET
    if abs(parsed.year - expected_be_year) <= _BE_YEAR_SLACK:
        try:
            parsed = parsed.replace(year=parsed.year - _BE_OFFSET)
        except ValueError:
            return None

    # dateparser ships no type stubs -- parsed is `Any` to mypy -- so the
    # explicit annotation here (not the return statement itself) is what
    # pins the actual return type back down to `str`.
    result: str = parsed.isoformat() if is_datetime else parsed.date().isoformat()
    return result


def match_choice(raw_text: str, choices: "list[Choice]") -> "Choice | None":
    """Resolves raw_text against a real OPTION/BOOLEAN question's choice
    list -- exact label match first (identical to this repo's behavior
    before this function existed, so every existing exact-match case,
    including the pause/skip/pagination sentinel labels, is completely
    unaffected), then a retry with trailing Thai politeness particles
    stripped ("ใช่ครับ" -> "ใช่").

    Returns None (never guesses) when neither matches -- the caller's
    existing "doesn't match any listed choice" re-ask path handles that
    exactly as it already does today.
    """
    exact = next((c for c in choices if c.label == raw_text), None)
    if exact is not None:
        return exact

    text = raw_text.strip()
    stripped_once = False
    changed = True
    while changed:
        changed = False
        for particle in _TRAILING_PARTICLES:
            if len(text) > len(particle) and text.endswith(particle):
                text = text[: -len(particle)].strip()
                stripped_once = True
                changed = True
                break
    if not stripped_once or not text:
        return None
    return next((c for c in choices if c.label == text), None)


def try_fixed_parse(rule_type: str, raw_text: str) -> str | None:
    """Dispatches to parse_number/parse_date by validation_rule["type"].
    Only ever called by service.py for a type already confirmed to be in
    FIXED_PARSE_TYPES.
    """
    if rule_type in ("INT", "FLOAT"):
        return parse_number(raw_text)
    return parse_date(raw_text, is_datetime=rule_type == "DATETIME")
