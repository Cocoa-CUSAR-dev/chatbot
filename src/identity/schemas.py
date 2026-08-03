from pydantic import BaseModel


class LinkCodeVerifyRequest(BaseModel):
    line_user_id: str
    code: str


class LinkResult(BaseModel):
    linked: bool
    user_id: str | None = None
