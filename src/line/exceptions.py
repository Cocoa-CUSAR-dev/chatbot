from fastapi import status

from src.exceptions import ServiceException


class InvalidLineSignature(ServiceException):
    status_code = status.HTTP_400_BAD_REQUEST
    detail = "Invalid LINE webhook signature"


class LineAccountNotLinked(ServiceException):
    """A verified LIFF session whose LINE user_id has no auth.line_identity
    row yet -- same "not linked" case router.py's webhook path replies to
    inline (see _resolve_user_id), just reachable from a LIFF HTTP call
    instead (docs-and-plan#176), where there's no reply_token to answer
    with a chat message -- the LIFF screen itself has to show this.
    """

    status_code = status.HTTP_403_FORBIDDEN
    detail = "บัญชี LINE นี้ยังไม่ได้เชื่อมกับบัญชีในระบบ"
