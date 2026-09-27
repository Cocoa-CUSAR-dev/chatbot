from src.conversation import text_parsing
from src.conversation.service import Choice


class TestParseNumber:
    def test_plain_int_passes_through_unchanged(self) -> None:
        assert text_parsing.parse_number("900") == "900"

    def test_plain_float_passes_through_unchanged(self) -> None:
        assert text_parsing.parse_number("12.5") == "12.5"

    def test_thai_digit_characters_parse_natively(self) -> None:
        # int()/float() handle ๐-๙ the same as ASCII digits -- no
        # translation table needed for this.
        assert text_parsing.parse_number("๙๐๐") == "๙๐๐"

    def test_comma_thousands_separator_is_stripped(self) -> None:
        assert text_parsing.parse_number("1,500") == "1500"

    def test_spelled_out_thai_number_is_converted(self) -> None:
        assert text_parsing.parse_number("เก้าร้อย") == "900"

    def test_compound_spelled_out_thai_number_is_converted(self) -> None:
        assert text_parsing.parse_number("เก้าร้อยห้าสิบเอ็ด") == "951"

    def test_garbage_text_returns_none(self) -> None:
        assert text_parsing.parse_number("กากๆ") is None

    def test_number_word_with_a_trailing_unit_returns_none(self) -> None:
        # Deliberately not handled -- see the module docstring: this stays
        # a narrow fallback for a bare spelled-out number, not a general
        # text-to-number extractor.
        assert text_parsing.parse_number("เก้าร้อยกิโล") is None

    def test_blank_returns_none(self) -> None:
        assert text_parsing.parse_number("   ") is None


class TestParseDate:
    def test_iso_date_passes_through_unchanged(self) -> None:
        assert text_parsing.parse_date("2026-09-17") == "2026-09-17"

    def test_relative_today(self) -> None:
        import datetime

        assert text_parsing.parse_date("วันนี้") == datetime.date.today().isoformat()

    def test_relative_tomorrow_and_yesterday_are_distinct(self) -> None:
        tomorrow = text_parsing.parse_date("พรุ่งนี้")
        yesterday = text_parsing.parse_date("เมื่อวาน")
        today = text_parsing.parse_date("วันนี้")
        assert yesterday is not None and today is not None and tomorrow is not None
        assert yesterday < today < tomorrow

    def test_buddhist_era_year_is_corrected_to_gregorian(self) -> None:
        # "2569" is what a farmer would naturally type/say for this date in
        # Thai convention -- dateparser has no Thai-calendar awareness and
        # returns it as a literal (nonsensical, 543 years out) Gregorian
        # year unless corrected.
        assert text_parsing.parse_date("15 มกราคม 2569") == "2026-01-15"

    def test_already_gregorian_year_is_not_touched(self) -> None:
        assert text_parsing.parse_date("15 มกราคม 2026") == "2026-01-15"

    def test_datetime_mode_returns_full_isoformat(self) -> None:
        result = text_parsing.parse_date("2026-09-17", is_datetime=True)
        assert result is not None
        assert result.startswith("2026-09-17")

    def test_garbage_text_returns_none(self) -> None:
        assert text_parsing.parse_date("กากๆ") is None

    def test_blank_returns_none(self) -> None:
        assert text_parsing.parse_date("   ") is None


class TestMatchChoice:
    def test_exact_match(self) -> None:
        choices = [Choice(id="true", label="ใช่"), Choice(id="false", label="ไม่")]
        assert text_parsing.match_choice("ใช่", choices) == choices[0]

    def test_trailing_particle_is_stripped_before_matching(self) -> None:
        choices = [Choice(id="true", label="ใช่"), Choice(id="false", label="ไม่")]
        assert text_parsing.match_choice("ใช่ครับ", choices) == choices[0]
        assert text_parsing.match_choice("ไม่ค่ะ", choices) == choices[1]

    def test_unrelated_text_returns_none(self) -> None:
        choices = [Choice(id="true", label="ใช่"), Choice(id="false", label="ไม่")]
        assert text_parsing.match_choice("อาจจะ", choices) is None

    def test_does_not_confuse_near_identical_enumerated_labels(self) -> None:
        """Regression: a naive similarity match would score "farm 15"
        very close to "farm 1" and silently resolve to the wrong one.
        Particle-stripping can't do that -- "farm 15" has no trailing
        particle to strip, so this must return None, not a wrong guess.
        """
        choices = [Choice(id=str(i), label=f"farm {i}") for i in range(20)]
        assert text_parsing.match_choice("farm 15", choices[:10]) is None


class TestTryFixedParse:
    def test_int_dispatches_to_parse_number(self) -> None:
        assert text_parsing.try_fixed_parse("INT", "เก้าร้อย") == "900"

    def test_float_dispatches_to_parse_number(self) -> None:
        assert text_parsing.try_fixed_parse("FLOAT", "12.5") == "12.5"

    def test_date_dispatches_to_parse_date(self) -> None:
        assert text_parsing.try_fixed_parse("DATE", "2026-09-17") == "2026-09-17"

    def test_datetime_dispatches_to_parse_date_in_datetime_mode(self) -> None:
        result = text_parsing.try_fixed_parse("DATETIME", "2026-09-17")
        assert result is not None
        assert result.startswith("2026-09-17")
