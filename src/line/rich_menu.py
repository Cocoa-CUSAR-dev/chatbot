"""Rich menu management -- e.g. the to-do fallback channel for farmers who
don't act on a reminder push (ADR 0006's flagged-but-not-yet-built fallback).
Not wired into a route yet -- these are the primitives; there's no committed
backlog task driving specific menu contents/layout yet.
"""

from linebot.v3.messaging import (
    AsyncApiClient,
    AsyncMessagingApi,
    AsyncMessagingApiBlob,
    Configuration,
    MessageAction,
    RichMenuArea,
    RichMenuBounds,
    RichMenuRequest,
    RichMenuSize,
)

from src.line.config import line_settings

_configuration = Configuration(access_token=line_settings.LINE_CHANNEL_ACCESS_TOKEN)


async def create_rich_menu(
    name: str, chat_bar_text: str, areas: list[tuple[RichMenuBounds, MessageAction]]
) -> str:
    """Returns the new rich_menu_id. Creating the menu and uploading its
    image are two separate LINE API calls -- see upload_rich_menu_image.
    """
    request = RichMenuRequest(
        size=RichMenuSize(width=2500, height=843),
        selected=False,
        name=name,
        chatBarText=chat_bar_text,
        areas=[RichMenuArea(bounds=bounds, action=action) for bounds, action in areas],
    )
    async with AsyncApiClient(_configuration) as client:
        response = await AsyncMessagingApi(client).create_rich_menu(request)
    return response.rich_menu_id


async def upload_rich_menu_image(rich_menu_id: str, image_bytes: bytes, content_type: str) -> None:
    """`content_type` must be image/jpeg or image/png -- LINE's own
    requirement, not this code's.
    """
    async with AsyncApiClient(_configuration) as client:
        await AsyncMessagingApiBlob(client).set_rich_menu_image(
            rich_menu_id, image_bytes, _content_type=content_type
        )


async def set_default_rich_menu(rich_menu_id: str) -> None:
    """Applies to every farmer who doesn't already have a menu explicitly
    linked via link_rich_menu_to_user.
    """
    async with AsyncApiClient(_configuration) as client:
        await AsyncMessagingApi(client).set_default_rich_menu(rich_menu_id)


async def link_rich_menu_to_user(user_id: str, rich_menu_id: str) -> None:
    async with AsyncApiClient(_configuration) as client:
        await AsyncMessagingApi(client).link_rich_menu_id_to_user(user_id, rich_menu_id)
