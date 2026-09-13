from src.tasks.config import tasks_settings


def test_tasks_settings_load_from_env() -> None:
    assert tasks_settings.GO_BACKEND_URL
    assert tasks_settings.GO_SERVICE_KEY
