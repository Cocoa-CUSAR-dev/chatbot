from src.conversation.constants import ActiveSubstate, AnswerSource, ConversationStatus


def test_conversation_status_values() -> None:
    assert ConversationStatus.ACTIVE == "active"
    assert ConversationStatus.PAUSED == "paused"
    assert ConversationStatus.COMPLETED == "completed"
    assert ConversationStatus.CANCELLED == "cancelled"


def test_active_substate_values() -> None:
    assert ActiveSubstate.AWAITING_INPUT == "awaiting_input"
    assert ActiveSubstate.LLM_EXTRACTING == "llm_extracting"
    assert ActiveSubstate.LLM_ASKING_FOLLOW_UP == "llm_asking_follow_up"
    assert ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION == "guided_asking_fixed_question"
    assert ActiveSubstate.GUIDED_AWAITING_FIXED_ANSWER == "guided_awaiting_fixed_answer"
    assert ActiveSubstate.AWAITING_CONFIRMATION == "awaiting_confirmation"


def test_answer_source_values() -> None:
    assert AnswerSource.LLM_EXTRACTED == "llm_extracted"
    assert AnswerSource.GUIDED_FLOW == "guided_flow"
