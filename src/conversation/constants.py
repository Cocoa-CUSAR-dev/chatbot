"""State names match target-architecture.md #4 exactly -- keep them in sync
if that diagram ever changes, and vice versa.
"""

from enum import StrEnum


class ConversationStatus(StrEnum):
    """Outer state machine -- persisted as chat_conversation.status."""

    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"


class ActiveSubstate(StrEnum):
    """Inner states, only meaningful while status == ACTIVE. Not persisted as
    its own column -- derivable from chat_conversation_answer rows plus
    whichever slots are still unfilled.
    """

    AWAITING_INPUT = "awaiting_input"

    # LLMConversation sub-machine -- while the LLM is healthy
    LLM_EXTRACTING = "llm_extracting"
    LLM_ASKING_FOLLOW_UP = "llm_asking_follow_up"

    # GuidedFlow sub-machine -- the real fallback, only on genuine LLM failure
    GUIDED_ASKING_FIXED_QUESTION = "guided_asking_fixed_question"
    GUIDED_AWAITING_FIXED_ANSWER = "guided_awaiting_fixed_answer"

    AWAITING_CONFIRMATION = "awaiting_confirmation"


class AnswerSource(StrEnum):
    """chat_conversation_answer.source -- lets a researcher later distinguish
    AI-extracted answers from guided-flow ones (US2-8).
    """

    LLM_EXTRACTED = "llm_extracted"
    GUIDED_FLOW = "guided_flow"
