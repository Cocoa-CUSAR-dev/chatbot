from fastapi import status

from src.exceptions import ServiceException


class LLMUnavailable(ServiceException):
    """Raised on genuine LLM failure/timeout -- this is exactly the signal that
    triggers the switch from LLMConversation into GuidedFlow
    (target-architecture.md #4). It is NOT raised for "partial extraction" --
    that's a normal, successful response with some fields unset.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    detail = "LLM provider unavailable"
