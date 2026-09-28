from fastapi import status

from src.exceptions import ServiceException


class ReminderNotFound(ServiceException):
    status_code = status.HTTP_404_NOT_FOUND
    detail = "Reminder schedule not found"


class InvalidReminderReference(ServiceException):
    """A task, role or user id in the request doesn't exist."""

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    detail = "The task, role or user in the request does not exist"
