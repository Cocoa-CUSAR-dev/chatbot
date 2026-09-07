from src.line.config import line_settings


def test_line_settings_load_from_env() -> None:
    assert line_settings.LINE_CHANNEL_SECRET
    assert line_settings.LINE_CHANNEL_ACCESS_TOKEN


def test_liff_id_defaults_to_empty_string() -> None:
    assert line_settings.LIFF_ID == ""
