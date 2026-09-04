from src.forms.config import forms_settings


def test_forms_settings_load_from_env() -> None:
    # Values come from tests/conftest.py's os.environ.setdefault calls.
    assert forms_settings.KOTLIN_BACKEND_URL
    assert forms_settings.KOTLIN_SERVICE_KEY
