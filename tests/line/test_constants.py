from src.line.constants import ErrorCode


def test_invalid_signature_code() -> None:
    assert ErrorCode.INVALID_SIGNATURE == "line/invalid_signature"
