"""Per-field answer format validation for GuidedFlow -- the (New) Validate
Answer step between GUIDED_AWAITING_FIXED_ANSWER and either the next
question or a re-ask with guidance on the expected format.

Rules should eventually come from Kotlin (form.field_validation_rule, keyed
by field_name), the same way question scripts already do via
forms/client.py's get_form(). That table isn't exposed over the API yet, so
_RULES below is a local, hand-copied mirror of the researcher side's own
seed data (see `INSERT INTO form.field_validation_rule` in this task's
history) standing in until it is. Swapping to a real lookup later only means
replacing `_RULES.get(field_name)` inside `validate_answer` -- its signature
(field_name, raw_text) -> error message or None doesn't need to change, so
nothing in service.py has to know which source is live.
"""

from datetime import date, datetime
from typing import Any, Protocol

_RULES: dict[str, dict[str, Any]] = {
    "amount": {"type": "FLOAT", "min": 0, "max": 1000, "errorMessage": "กรุณากรอกปริมาณ 0-1,000"},
    "bean_color_inside": {
        "type": "VARCHAR",
        "maxLength": 200,
        "errorMessage": "กรุณากรอกไม่เกิน 200 ตัวอักษร",
    },
    "bean_color_outside": {
        "type": "VARCHAR",
        "maxLength": 200,
        "errorMessage": "กรุณากรอกไม่เกิน 200 ตัวอักษร",
    },
    "cut_test_result": {
        "type": "VARCHAR",
        "maxLength": 500,
        "errorMessage": "กรุณากรอกไม่เกิน 500 ตัวอักษร",
    },
    "description": {
        "type": "VARCHAR",
        "maxLength": 500,
        "errorMessage": "กรุณากรอกไม่เกิน 500 ตัวอักษร",
    },
    "drying_facility_type_code": {
        "type": "VARCHAR",
        "maxLength": 200,
        "errorMessage": "กรุณากรอกไม่เกิน 200 ตัวอักษร",
    },
    "ends_at": {"type": "DATETIME", "maxDate": "today", "errorMessage": "ห้ามระบุเวลาที่ยังไม่ถึง"},
    "fan_count": {
        "type": "INT",
        "min": 0,
        "max": 50,
        "integerOnly": True,
        "errorMessage": "กรุณากรอกจำนวนพัดลมเป็นจำนวนเต็ม 0-50",
    },
    "fan_power": {
        "type": "FLOAT",
        "min": 0,
        "max": 5000,
        "errorMessage": "กรุณากรอกกำลังไฟฟ้า 0-5,000 (วัตต์)",
    },
    "gis": {"type": "GEODATA", "validLatLng": True, "errorMessage": "พิกัดไม่ถูกต้อง"},
    "harvest_date": {
        "type": "DATE",
        "maxDate": "today",
        "errorMessage": "ห้ามระบุวันที่ในอนาคต",
    },
    "humi": {"type": "FLOAT", "min": 0, "max": 100, "errorMessage": "ความชื้นต้องอยู่ระหว่าง 0-100%"},
    "logistic_result": {
        "type": "VARCHAR",
        "maxLength": 500,
        "errorMessage": "กรุณากรอกไม่เกิน 500 ตัวอักษร",
    },
    "management_method": {
        "type": "VARCHAR",
        "maxLength": 500,
        "errorMessage": "กรุณากรอกไม่เกิน 500 ตัวอักษร",
    },
    "notes": {"type": "VARCHAR", "maxLength": 500, "errorMessage": "กรุณากรอกไม่เกิน 500 ตัวอักษร"},
    "quantity_kg": {
        "type": "FLOAT",
        "min": 0,
        "max": 5000,
        "errorMessage": "กรุณากรอกปริมาณ 0-5,000 (กก.)",
    },
    "smell": {"type": "VARCHAR", "maxLength": 200, "errorMessage": "กรุณากรอกไม่เกิน 200 ตัวอักษร"},
    "started_at": {
        "type": "DATETIME",
        "maxDate": "today",
        "errorMessage": "ห้ามระบุเวลาที่ยังไม่ถึง",
    },
    "tank_volume_liter": {
        "type": "FLOAT",
        "min": 0,
        "max": 10000,
        "errorMessage": "กรุณากรอกขนาดถัง 0-10,000 (ลิตร)",
    },
    "temp_inside": {
        "type": "FLOAT",
        "min": 0,
        "max": 100,
        "errorMessage": "กรุณากรอกอุณหภูมิ 0-100 (องศาเซลเซียส)",
    },
    "temp_outside": {
        "type": "FLOAT",
        "min": 0,
        "max": 60,
        "errorMessage": "กรุณากรอกอุณหภูมิ 0-60 (องศาเซลเซียส)",
    },
    "weather_condition_code": {
        "type": "VARCHAR",
        "maxLength": 200,
        "errorMessage": "กรุณากรอกไม่เกิน 200 ตัวอักษร",
    },
    "weight_gram_per_pod": {
        "type": "FLOAT",
        "min": 0,
        "max": 500,
        "errorMessage": "กรุณากรอกน้ำหนัก 0-500 (กรัม)",
    },
}


class _Validator(Protocol):
    def __call__(self, text: str, rule: dict[str, Any]) -> bool: ...


def _valid_float(text: str, rule: dict[str, Any]) -> bool:
    try:
        value = float(text.strip())
    except ValueError:
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
    max_length = rule.get("maxLength")
    return max_length is None or len(text) <= max_length


def _valid_date(text: str, rule: dict[str, Any]) -> bool:
    try:
        value = date.fromisoformat(text.strip())
    except ValueError:
        return False
    return not (rule.get("maxDate") == "today" and value > date.today())


def _valid_datetime(text: str, rule: dict[str, Any]) -> bool:
    try:
        value = datetime.fromisoformat(text.strip())
    except ValueError:
        return False
    return not (rule.get("maxDate") == "today" and value > datetime.now())


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
    if not rule.get("validLatLng"):
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


def validate_answer(field_name: str, raw_text: str) -> str | None:
    """None if `raw_text` satisfies field_name's rule, or if the field has no
    rule at all -- unvalidated fields pass through unchanged, same as before
    this existed. Otherwise returns the rule's own errorMessage, ready to
    show the farmer as-is alongside the re-asked question.
    """
    rule = _RULES.get(field_name)
    if rule is None:
        return None
    validator = _VALIDATORS.get(rule.get("type", ""))
    if validator is None or validator(raw_text, rule):
        return None
    return str(rule.get("errorMessage") or "รูปแบบคำตอบไม่ถูกต้อง กรุณาลองใหม่อีกครั้ง")
