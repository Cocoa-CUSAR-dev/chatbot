"""Per-field answer format validation for GuidedFlow -- the (New) Validate
Answer step between GUIDED_AWAITING_FIXED_ANSWER and either the next
question or a re-ask with guidance on the expected format.

Rules come from Kotlin's form.field_validation_rule, keyed by field_name and
joined onto each question server-side (web-backend's FormRepository.kt --
queryFormRecords LEFT JOINs FIELD_VALIDATION_RULE on field_name,
toQuestionEntity maps it into Question.Entity.validationRule). GET
/service/forms/{formId} (the exact route forms/client.py's get_form() calls)
returns it null for OPTION/BOOLEAN/upload questions (already constrained
another way) and as the rule's own JSON object otherwise -- validate_answer
below takes that dict directly, already attached to the question by
service.py's _question_from_dict, no lookup needed here.

Mind the key casing: forms/client.py's _convert_keys recursively
camelCase -> snake_cases *every* nested dict in Kotlin's response, including
the contents of validationRule itself, not just the outer question/section
keys it exists for. So by the time a rule dict reaches this module, its keys
are already error_message/max_length/integer_only/max_date/valid_lat_lng --
not the jsonb column's own camelCase spelling (type/min/max are single
words, unaffected). Reading the camelCase spelling here would silently
never match, letting every answer through unvalidated.
"""

import math
from datetime import date, datetime
from typing import Any, Protocol


class _Validator(Protocol):
    def __call__(self, text: str, rule: dict[str, Any]) -> bool: ...


def _valid_float(text: str, rule: dict[str, Any]) -> bool:
    try:
        value = float(text.strip())
    except ValueError:
        return False
    # float("nan") parses without raising, and every comparison against NaN
    # (< and >) is False -- so without this check, "nan" silently satisfies
    # both the min and max bounds below regardless of what they are.
    if math.isnan(value):
        return False
    if "min" in rule and value < rule["min"]:
        return False
    return not ("max" in rule and value > rule["max"])


def _valid_int(text: str, rule: dict[str, Any]) -> bool:
    # Every INT rule in the current seed sets integerOnly: true (there's no
    # "INT but decimals are fine" case to support), so this always requires
    # a clean int() parse rather than branching on the flag's value.
    try:
        value = int(text.strip())
    except ValueError:
        return False
    if "min" in rule and value < rule["min"]:
        return False
    return not ("max" in rule and value > rule["max"])


def _valid_varchar(text: str, rule: dict[str, Any]) -> bool:
    max_length = rule.get("max_length")
    return max_length is None or len(text) <= max_length


def _valid_date(text: str, rule: dict[str, Any]) -> bool:
    try:
        value = date.fromisoformat(text.strip())
    except ValueError:
        return False
    return not (rule.get("max_date") == "today" and value > date.today())


def _valid_datetime(text: str, rule: dict[str, Any]) -> bool:
    try:
        value = datetime.fromisoformat(text.strip())
    except ValueError:
        return False
    return not (rule.get("max_date") == "today" and value > datetime.now())


def _valid_geodata(text: str, rule: dict[str, Any]) -> bool:
    # "lat,lng" -- matches how the LIFF-side map picker already hands a
    # coordinate pair back as a single string field.
    parts = text.strip().split(",")
    if len(parts) != 2:
        return False
    try:
        lat, lng = float(parts[0].strip()), float(parts[1].strip())
    except ValueError:
        return False
    if not rule.get("valid_lat_lng"):
        return True
    return -90 <= lat <= 90 and -180 <= lng <= 180


_VALIDATORS: dict[str, _Validator] = {
    "FLOAT": _valid_float,
    "INT": _valid_int,
    "VARCHAR": _valid_varchar,
    "DATE": _valid_date,
    "DATETIME": _valid_datetime,
    "GEODATA": _valid_geodata,
}


def validate_answer(rule: dict[str, Any] | None, raw_text: str) -> str | None:
    """None if `raw_text` satisfies `rule`, or if `rule` is None -- questions
    with no validation_rule (OPTION/BOOLEAN/upload, or any field_name with
    no row in form.field_validation_rule) pass through unchanged. Otherwise
    returns the rule's own error_message, ready to show the farmer as-is
    alongside the re-asked question.
    """
    if rule is None:
        return None
    validator = _VALIDATORS.get(rule.get("type", ""))
    if validator is None or validator(raw_text, rule):
        return None
    return str(rule.get("error_message") or "รูปแบบคำตอบไม่ถูกต้อง กรุณาลองใหม่อีกครั้ง")
