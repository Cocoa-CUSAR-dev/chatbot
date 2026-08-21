from src.conversation.validation import validate_answer


def test_unknown_field_has_no_rule_and_always_passes() -> None:
    assert validate_answer("some_untracked_field", "literally anything") is None


class TestFloat:
    def test_within_range_passes(self) -> None:
        assert validate_answer("humi", "55.5") is None

    def test_below_min_fails_with_error_message(self) -> None:
        assert validate_answer("humi", "-1") == "ความชื้นต้องอยู่ระหว่าง 0-100%"

    def test_above_max_fails(self) -> None:
        assert validate_answer("humi", "101") is not None

    def test_non_numeric_fails(self) -> None:
        assert validate_answer("humi", "abc") is not None

    def test_boundary_values_pass(self) -> None:
        assert validate_answer("humi", "0") is None
        assert validate_answer("humi", "100") is None


class TestInt:
    def test_within_range_passes(self) -> None:
        assert validate_answer("fan_count", "5") is None

    def test_decimal_rejected_even_if_in_range(self) -> None:
        # integerOnly: true -- "5.0" must not silently become 5.
        assert validate_answer("fan_count", "5.0") is not None

    def test_above_max_fails(self) -> None:
        assert validate_answer("fan_count", "51") == "กรุณากรอกจำนวนพัดลมเป็นจำนวนเต็ม 0-50"

    def test_non_numeric_fails(self) -> None:
        assert validate_answer("fan_count", "หลายตัว") is not None


class TestVarchar:
    def test_within_max_length_passes(self) -> None:
        assert validate_answer("notes", "x" * 500) is None

    def test_over_max_length_fails(self) -> None:
        assert validate_answer("notes", "x" * 501) == "กรุณากรอกไม่เกิน 500 ตัวอักษร"

    def test_empty_string_passes(self) -> None:
        # No minLength in the current rule set -- empty text is a length-0
        # string, which satisfies maxLength same as any other.
        assert validate_answer("notes", "") is None


class TestDate:
    def test_past_date_passes(self) -> None:
        assert validate_answer("harvest_date", "2020-01-01") is None

    def test_future_date_fails(self) -> None:
        assert validate_answer("harvest_date", "2099-01-01") == "ห้ามระบุวันที่ในอนาคต"

    def test_unparseable_text_fails(self) -> None:
        assert validate_answer("harvest_date", "yesterday") is not None


class TestDatetime:
    def test_past_datetime_passes(self) -> None:
        assert validate_answer("started_at", "2020-01-01T08:00:00") is None

    def test_future_datetime_fails(self) -> None:
        assert validate_answer("started_at", "2099-01-01T08:00:00") == "ห้ามระบุเวลาที่ยังไม่ถึง"


class TestGeodata:
    def test_valid_lat_lng_passes(self) -> None:
        assert validate_answer("gis", "13.7563,100.5018") is None

    def test_valid_lat_lng_with_space_passes(self) -> None:
        assert validate_answer("gis", "13.7563, 100.5018") is None

    def test_out_of_range_lat_fails(self) -> None:
        assert validate_answer("gis", "999,100.5018") == "พิกัดไม่ถูกต้อง"

    def test_missing_component_fails(self) -> None:
        assert validate_answer("gis", "13.7563") is not None

    def test_non_numeric_fails(self) -> None:
        assert validate_answer("gis", "bangkok") is not None
