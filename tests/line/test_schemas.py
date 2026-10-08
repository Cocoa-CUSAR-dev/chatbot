from src.line.schemas import QuickReplyOption


def test_quick_reply_option_round_trip() -> None:
    option = QuickReplyOption(label="ใช่", text="ใช่")
    assert option.label == "ใช่"
    assert option.text == "ใช่"


def test_option_defaults_to_a_message_action() -> None:
    """Every caller that predates GEODATA builds options without `kind` --
    they must keep meaning "send this text".
    """
    assert QuickReplyOption(label="ใช่", text="ใช่").kind == "message"


def test_location_option_needs_no_text() -> None:
    assert QuickReplyOption(label="📍 ส่งตำแหน่ง", kind="location").text == ""
