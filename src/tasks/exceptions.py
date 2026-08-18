from fastapi import status

from src.exceptions import ServiceException, UpstreamServiceError

__all__ = ["HandlerNotSupported", "UpstreamServiceError"]


class HandlerNotSupported(ServiceException):
    """Go returned 501 -- this handler's dissection logic hasn't been built
    yet (see docs/plans/chatbot-child-handler-design.md). Deliberately NOT
    an UpstreamServiceError: that class means one of the backends is broken
    or unreachable, but here Go is working exactly as intended and telling
    us plainly "not yet." Retrying can never succeed, unlike a genuine
    transient failure -- confirm_conversation catches this specifically so
    the farmer gets told that honestly instead of a generic "try again."
    """

    status_code = status.HTTP_501_NOT_IMPLEMENTED
    detail = "This form type can't be saved automatically yet"
