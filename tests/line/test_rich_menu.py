from unittest.mock import AsyncMock, MagicMock, patch

from linebot.v3.messaging import MessageAction, RichMenuBounds

from src.line import rich_menu


def _patched_api_client():
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return patch("src.line.rich_menu.AsyncApiClient", return_value=client)


async def test_create_rich_menu_returns_the_new_menu_id() -> None:
    messaging_api = MagicMock()
    messaging_api.create_rich_menu = AsyncMock(
        return_value=MagicMock(rich_menu_id="richmenu-abc123")
    )
    bounds = RichMenuBounds(x=0, y=0, width=2500, height=843)
    action = MessageAction(label="เมนู", text="เมนู")

    with (
        _patched_api_client(),
        patch("src.line.rich_menu.AsyncMessagingApi", return_value=messaging_api),
    ):
        menu_id = await rich_menu.create_rich_menu("main", "เมนูหลัก", [(bounds, action)])

    assert menu_id == "richmenu-abc123"
    messaging_api.create_rich_menu.assert_awaited_once()


async def test_upload_rich_menu_image_calls_set_rich_menu_image() -> None:
    blob_api = MagicMock()
    blob_api.set_rich_menu_image = AsyncMock()

    with (
        _patched_api_client(),
        patch("src.line.rich_menu.AsyncMessagingApiBlob", return_value=blob_api),
    ):
        await rich_menu.upload_rich_menu_image("richmenu-abc123", b"\x89PNG", "image/png")

    blob_api.set_rich_menu_image.assert_awaited_once_with(
        "richmenu-abc123", b"\x89PNG", _content_type="image/png"
    )


async def test_set_default_rich_menu_calls_the_right_endpoint() -> None:
    messaging_api = MagicMock()
    messaging_api.set_default_rich_menu = AsyncMock()

    with (
        _patched_api_client(),
        patch("src.line.rich_menu.AsyncMessagingApi", return_value=messaging_api),
    ):
        await rich_menu.set_default_rich_menu("richmenu-abc123")

    messaging_api.set_default_rich_menu.assert_awaited_once_with("richmenu-abc123")


async def test_link_rich_menu_to_user_calls_the_right_endpoint() -> None:
    messaging_api = MagicMock()
    messaging_api.link_rich_menu_id_to_user = AsyncMock()

    with (
        _patched_api_client(),
        patch("src.line.rich_menu.AsyncMessagingApi", return_value=messaging_api),
    ):
        await rich_menu.link_rich_menu_to_user("Uabc123", "richmenu-abc123")

    messaging_api.link_rich_menu_id_to_user.assert_awaited_once_with("Uabc123", "richmenu-abc123")
