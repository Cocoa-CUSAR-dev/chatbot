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
