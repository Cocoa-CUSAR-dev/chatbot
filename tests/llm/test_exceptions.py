from fastapi import status

from src.exceptions import ServiceException
from src.llm.exceptions import LLMUnavailable


def test_llm_unavailable_defaults() -> None:
    exc = LLMUnavailable()
    assert exc.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert exc.detail == "LLM provider unavailable"
    assert isinstance(exc, ServiceException)
