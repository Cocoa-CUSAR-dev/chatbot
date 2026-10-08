from unittest.mock import AsyncMock, patch

from src.line import router
from src.line.config import line_settings


# US3-2 / #125 (F2): the diary card's "view full history" deep link must carry
# the SSO token in the URL *fragment* (`#token=`), never the query string
# (`?token=`) -- the fragment is never sent to the server, so the token can't
# land in access logs or a Referer header. Red before the fix (was `?token=`),
# green after.
async def test_diary_card_link_puts_token_in_fragment_not_query() -> None:
    captured: dict[str, str] = {}

    def fake_build_diary_flex(diary_text: str, history_url: str) -> dict:
        captured["history_url"] = history_url
        return {"type": "flex"}

    with (
        patch("src.line.router.generate_diary", AsyncMock(return_value="diary text")),
        patch("src.line.router.mint_sso_token", AsyncMock(return_value="TOK123")),
        patch("src.line.router.build_diary_flex", side_effect=fake_build_diary_flex),
        patch("src.line.router.push_flex", AsyncMock()),
    ):
        await router._generate_and_push_diary("user-1", "Uabc")

    url = captured["history_url"]
    assert url == f"{line_settings.WEB_APP_URL}/sso#token=TOK123"
    assert "?token=" not in url
