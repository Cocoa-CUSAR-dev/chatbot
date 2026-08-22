from src.conversation.validation import validate_answer
from src.forms.client import _convert_keys


def test_none_rule_always_passes() -> None:
    # OPTION/BOOLEAN/upload questions, or any field_name with no row in
    # form.field_validation_rule -- Kotlin sends validationRule: null for
    # these, which arrives here as rule=None.
    assert validate_answer(None, "literally anything") is None


class TestFloat:
    _RULE = {"type": "FLOAT", "min": 0, "max": 100, "error_message": "ความชื้นต้องอยู่ระหว่าง 0-100%"}

    def test_within_range_passes(self) -> None:
        assert validate_answer(self._RULE, "55.5") is None

    def test_below_min_fails_with_error_message(self) -> None:
        assert validate_answer(self._RULE, "-1") == "ความชื้นต้องอยู่ระหว่าง 0-100%"

    def test_above_max_fails(self) -> None:
        assert validate_answer(self._RULE, "101") is not None

    def test_non_numeric_fails(self) -> None:
        assert validate_answer(self._RULE, "abc") is not None

    def test_boundary_values_pass(self) -> None:
        assert validate_answer(self._RULE, "0") is None
        assert validate_answer(self._RULE, "100") is None


class TestInt:
    _RULE = {
        "type": "INT",
        "min": 0,
        "max": 50,
        "integer_only": True,
        "error_message": "กรุณากรอกจำนวนพัดลมเป็นจำนวนเต็ม 0-50",
    }

    def test_within_range_passes(self) -> None:
        assert validate_answer(self._RULE, "5") is None

    def test_decimal_rejected_even_if_in_range(self) -> None:
        # integer_only: true -- "5.0" must not silently become 5.
        assert validate_answer(self._RULE, "5.0") is not None

    def test_above_max_fails(self) -> None:
        assert validate_answer(self._RULE, "51") == "กรุณากรอกจำนวนพัดลมเป็นจำนวนเต็ม 0-50"

    def test_non_numeric_fails(self) -> None:
        assert validate_answer(self._RULE, "หลายตัว") is not None


class TestVarchar:
    _RULE = {"type": "VARCHAR", "max_length": 500, "error_message": "กรุณากรอกไม่เกิน 500 ตัวอักษร"}

    def test_within_max_length_passes(self) -> None:
        assert validate_answer(self._RULE, "x" * 500) is None

    def test_over_max_length_fails(self) -> None:
        assert validate_answer(self._RULE, "x" * 501) == "กรุณากรอกไม่เกิน 500 ตัวอักษร"

    def test_empty_string_passes(self) -> None:
        # No min_length in the current rule set -- empty text is a length-0
        # string, which satisfies max_length same as any other.
        assert validate_answer(self._RULE, "") is None


class TestDate:
    _RULE = {"type": "DATE", "max_date": "today", "error_message": "ห้ามระบุวันที่ในอนาคต"}

    def test_past_date_passes(self) -> None:
        assert validate_answer(self._RULE, "2020-01-01") is None

    def test_future_date_fails(self) -> None:
        assert validate_answer(self._RULE, "2099-01-01") == "ห้ามระบุวันที่ในอนาคต"

    def test_unparseable_text_fails(self) -> None:
        assert validate_answer(self._RULE, "yesterday") is not None


class TestDatetime:
    _RULE = {"type": "DATETIME", "max_date": "today", "error_message": "ห้ามระบุเวลาที่ยังไม่ถึง"}

    def test_past_datetime_passes(self) -> None:
        assert validate_answer(self._RULE, "2020-01-01T08:00:00") is None

    def test_future_datetime_fails(self) -> None:
        assert validate_answer(self._RULE, "2099-01-01T08:00:00") == "ห้ามระบุเวลาที่ยังไม่ถึง"


class TestGeodata:
    _RULE = {"type": "GEODATA", "valid_lat_lng": True, "error_message": "พิกัดไม่ถูกต้อง"}

    def test_valid_lat_lng_passes(self) -> None:
        assert validate_answer(self._RULE, "13.7563,100.5018") is None

    def test_valid_lat_lng_with_space_passes(self) -> None:
        assert validate_answer(self._RULE, "13.7563, 100.5018") is None

    def test_out_of_range_lat_fails(self) -> None:
        assert validate_answer(self._RULE, "999,100.5018") == "พิกัดไม่ถูกต้อง"

    def test_missing_component_fails(self) -> None:
        assert validate_answer(self._RULE, "13.7563") is not None

    def test_non_numeric_fails(self) -> None:
        assert validate_answer(self._RULE, "bangkok") is not None


def test_real_camel_case_shape_survives_convert_keys_and_validates() -> None:
    """Locks in the actual wrinkle: Kotlin's jsonb column (and therefore its
    JSON response) spells these keys errorMessage/maxLength, not
    error_message/max_length -- forms/client.py's _convert_keys is what
    renames them before validate_answer ever sees the dict. If that
    conversion step were ever skipped, or this module reverted to reading
    the camelCase spelling, every answer would silently pass unvalidated
    instead of failing loudly.
    """
    kotlin_shaped_rule = {
        "type": "INT",
        "min": 0,
        "max": 50,
        "integerOnly": True,
        "errorMessage": "0-50 เท่านั้น",
    }

    converted_rule = _convert_keys(kotlin_shaped_rule)

    assert validate_answer(converted_rule, "999") == "0-50 เท่านั้น"
    assert validate_answer(converted_rule, "25") is None
