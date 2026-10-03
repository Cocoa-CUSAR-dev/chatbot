"""Flex Message layouts for the daily diary feature (US2-6,
docs-and-plan#133). Kept separate from src/line/service.py, which only
sends messages and never builds their content -- the JSON structure lives
in one place here instead.
"""

import logging
from typing import Any

from src.conversation.constants import PAUSE_LABEL

logger = logging.getLogger(__name__)

# LINE truncates a button label past 20 characters, so a long plot name is
# clipped here rather than silently by the client.
_FLEX_LABEL_MAX = 20


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

    `history_url` is usually a token-bearing SSO deep link (src/sso/client.py)
    so tapping it opens the farmer's history already logged in; it falls
    back to a plain link if minting that token failed (see
    _generate_and_push_diary in src/line/router.py).
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


# A bubble stays readable at about this many plot buttons; beyond it the
# builder splits into a carousel, which LINE itself caps at 12 bubbles.
_PLOTS_PER_BUBBLE = 10
_MAX_BUBBLES = 12


def _plot_button(label: str, data: str, *, selected: bool) -> dict[str, Any]:
    """One plot row. A sent Flex message can never be edited, so a tapped
    button cannot visually change -- the ✅ prefix here only reflects the
    selection as it was when THIS bubble was sent (e.g. after a resume). The
    running selection is reported in the short text reply each tap gets
    instead; see src/line/service.py's reply_plot_selection.
    """
    return {
        "type": "button",
        "style": "primary" if selected else "secondary",
        "color": "#4A7C59" if selected else "#F0F0F0",
        "height": "sm",
        "margin": "sm",
        "action": {
            "type": "postback",
            "label": (f"✅ {label}" if selected else label)[:_FLEX_LABEL_MAX],
            "data": data,
            "displayText": label,
        },
    }


def build_multi_plot_flex(
    title: str,
    options: list[tuple[str, str]],
    conversation_id: str,
    *,
    selected_ids: frozenset[str] = frozenset(),
    allow_whole_farm: bool = True,
) -> dict[str, Any]:
    """The multi-plot picker: one tappable button per plot, plus the actions
    that end the selection.

    `options` is [(plot_id, label)] already scoped to this farmer by
    src/line/plot_picker.py. Every button is a postback, because a
    MessageAction would put the plot name into the chat as if the farmer had
    typed it and the router would then have to tell a tap apart from typing.

    Why a Flex bubble and not Quick Reply, which the rest of this flow uses:
    Quick Reply buttons vanish as soon as any newer message arrives, and this
    picker must survive its own "เลือกแล้ว: ..." confirmations -- the farmer
    taps it several times in a row. The trade-off is that the bubble stays
    tappable forever, including after the question has moved on, which is
    exactly why every one of these postbacks is checked against the
    conversation's current state before it changes anything (the stale-bubble
    guard in src/conversation/service.py).

    Too many plots for one bubble become a carousel; past the carousel's own
    12-bubble ceiling the remainder is dropped and logged, which is why
    plot_picker caps the query well below that.
    """
    chunks = [
        options[index : index + _PLOTS_PER_BUBBLE]
        for index in range(0, len(options), _PLOTS_PER_BUBBLE)
    ] or [[]]
    if len(chunks) > _MAX_BUBBLES:
        logger.warning(
            "multi-plot picker got %d plots, more than %d bubbles' worth -- showing the first %d",
            len(options),
            _MAX_BUBBLES,
            _MAX_BUBBLES * _PLOTS_PER_BUBBLE,
        )
        chunks = chunks[:_MAX_BUBBLES]

    bubbles = [
        _plot_bubble(
            title,
            chunk,
            conversation_id,
            selected_ids=selected_ids,
            allow_whole_farm=allow_whole_farm,
            # The closing actions go on the last bubble only: repeating
            # "เสร็จ" on every page of a carousel invites a farmer to end the
            # selection before scrolling to the plots they still wanted.
            with_actions=index == len(chunks) - 1,
            page=(index + 1, len(chunks)),
        )
        for index, chunk in enumerate(chunks)
    ]
    if len(bubbles) == 1:
        return bubbles[0]
    return {"type": "carousel", "contents": bubbles}


def _plot_bubble(
    title: str,
    options: list[tuple[str, str]],
    conversation_id: str,
    *,
    selected_ids: frozenset[str],
    allow_whole_farm: bool,
    with_actions: bool,
    page: tuple[int, int],
) -> dict[str, Any]:
    current, total = page
    heading = title if total == 1 else f"{title} ({current}/{total})"
    body: list[dict[str, Any]] = [
        {"type": "text", "text": heading, "weight": "bold", "wrap": True},
        {
            "type": "text",
            "text": 'กดเลือกได้หลายแปลง แล้วกด "เสร็จ"',
            "size": "sm",
            "color": "#888888",
            "wrap": True,
        },
        {"type": "separator", "margin": "md"},
    ]
    body += [
        _plot_button(
            label,
            f"plot_toggle:{conversation_id}:{plot_id}",
            selected=plot_id in selected_ids,
        )
        for plot_id, label in options
    ]

    bubble: dict[str, Any] = {
        "type": "bubble",
        "size": "mega",
        "body": {"type": "box", "layout": "vertical", "contents": body},
    }
    if not with_actions:
        return bubble

    footer: list[dict[str, Any]] = []
    if allow_whole_farm:
        # Same rule as the Quick Reply skip button: only a question the
        # researchers made optional may be answered with "the whole farm".
        footer.append(
            {
                "type": "button",
                "style": "link",
                "height": "sm",
                "action": {
                    "type": "postback",
                    "label": "ทั้งฟาร์ม",
                    "data": f"plot_whole_farm:{conversation_id}",
                    "displayText": "ทั้งฟาร์ม",
                },
            }
        )
    footer.append(
        {
            "type": "button",
            "style": "primary",
            "color": "#4A7C59",
            "height": "sm",
            "action": {
                "type": "postback",
                "label": "✅ เสร็จ",
                "data": f"plot_done:{conversation_id}",
                "displayText": "เสร็จ",
            },
        }
    )
    footer.append(
        {
            "type": "button",
            "style": "link",
            "height": "sm",
            "action": {
                # Pausing mid-selection has to be possible from the bubble
                # too, and it is the one action here that is NOT new: it
                # sends the same label handle_answer already treats as a
                # pause from any step.
                "type": "message",
                "label": PAUSE_LABEL,
                "text": PAUSE_LABEL,
            },
        }
    )
    bubble["footer"] = {"type": "box", "layout": "vertical", "contents": footer}
    return bubble
