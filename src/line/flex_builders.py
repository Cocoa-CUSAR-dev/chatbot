"""Flex Message layouts for the daily diary feature (US2-6,
docs-and-plan#133). Kept separate from src/line/service.py, which only
sends messages and never builds their content -- the JSON structure lives
in one place here instead.
"""

from typing import Any


def build_quick_ack_flex(text: str) -> dict[str, Any]:
    """Small bubble replacing the old plain-text "บันทึกข้อมูลเรียบร้อยแล้ว"
    reply -- sent immediately via reply_flex, before diary generation (which
    takes a few seconds for the LLM polish pass) has even started. See
    src/line/router.py's _generate_and_push_diary for the follow-up
    push_flex once the diary itself is ready.

    A green checkmark circle + text, same success-icon shape as the web
    app's CustomToast, so a confirm feels the same on both surfaces.
    """
    return {
        "type": "bubble",
        "size": "kilo",
        "body": {
            "type": "box",
            "layout": "horizontal",
            "spacing": "md",
            "alignItems": "center",
            "paddingAll": "lg",
            "contents": [
                {
                    "type": "box",
                    "layout": "vertical",
                    "width": "36px",
                    "height": "36px",
                    "cornerRadius": "18px",
                    "backgroundColor": "#4A7C59",
                    "justifyContent": "center",
                    "alignItems": "center",
                    "contents": [
                        {
                            "type": "text",
                            "text": "✓",
                            "color": "#ffffff",
                            "weight": "bold",
                            "size": "lg",
                            "align": "center",
                            "gravity": "center",
                        },
                    ],
                },
                {
                    "type": "text",
                    "text": f"🌱 {text}",
                    "wrap": True,
                    "weight": "bold",
                    "gravity": "center",
                    "flex": 1,
                },
            ],
        },
    }


def build_diary_flex(diary_text: str, history_url: str) -> dict[str, Any]:
    """The diary card pushed once generation finishes -- see
    src/line/router.py's _generate_and_push_diary. `diary_text` is already
    fully generated (template + LLM polish) by web-backend's DiaryService;
    this only lays it out, never edits it.

    `history_url` is a plain link (see line_settings.WEB_APP_URL's own
    comment on why -- US3-2's real SSO isn't built yet), not a token-bearing
    deep link.
    """
    return {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#4A7C59",
            "paddingAll": "md",
            "contents": [
                {
                    "type": "text",
                    "text": "ไดอารี่วันนี้",
                    "weight": "bold",
                    "color": "#ffffff",
                },
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": diary_text,
                    "wrap": True,
                },
            ],
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "color": "#4A7C59",
                    "action": {
                        "type": "uri",
                        "label": "ดูประวัติทั้งหมด",
                        "uri": history_url,
                    },
                },
            ],
        },
    }
