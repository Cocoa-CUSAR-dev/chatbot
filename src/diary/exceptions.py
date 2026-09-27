from fastapi import status

from src.exceptions import ServiceException, UpstreamServiceError

__all__ = ["DiaryNotAvailable", "UpstreamServiceError"]


class DiaryNotAvailable(ServiceException):
    """Kotlin returned 404 -- no submissions today for this user_id yet
    (e.g. a race between confirm_conversation's own DB commit and this
    call). Not an UpstreamServiceError: Kotlin is behaving correctly here,
    there's just nothing to generate a diary from right now.
    """

    status_code = status.HTTP_404_NOT_FOUND
    detail = "No diary could be generated -- no submissions found for today"
