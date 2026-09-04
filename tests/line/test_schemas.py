from src.line.schemas import QuickReplyOption


def test_quick_reply_option_round_trip() -> None:
    option = QuickReplyOption(label="ใช่", text="ใช่")
    assert option.label == "ใช่"
    assert option.text == "ใช่"
