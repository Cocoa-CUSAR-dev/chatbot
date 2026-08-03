from src.conversation.constants import ActiveSubstate
from src.conversation.state_machine import (
    on_extraction_result,
    on_guided_answer,
    on_llm_failed,
    on_llm_recovered,
)


def test_partial_extraction_does_not_trigger_guided_flow() -> None:
    """This is the exact bug ADR 0004's amendment corrected -- "still missing
    slots" must never by itself land in GuidedFlow.
    """
    transition = on_extraction_result(all_slots_filled=False)
    assert transition.next_state == ActiveSubstate.LLM_ASKING_FOLLOW_UP
    assert transition.next_state != ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION


def test_full_extraction_goes_to_confirmation() -> None:
    transition = on_extraction_result(all_slots_filled=True)
    assert transition.next_state == ActiveSubstate.AWAITING_CONFIRMATION


def test_llm_failure_triggers_guided_flow() -> None:
    transition = on_llm_failed()
    assert transition.next_state == ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION


def test_llm_recovery_returns_to_llm_conversation() -> None:
    """Bidirectional switch -- see target-architecture.md #4."""
    transition = on_llm_recovered()
    assert transition.next_state == ActiveSubstate.LLM_ASKING_FOLLOW_UP


def test_guided_answer_loops_until_all_slots_filled() -> None:
    assert on_guided_answer(all_slots_filled=False).next_state == (
        ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION
    )
    assert on_guided_answer(all_slots_filled=True).next_state == (
        ActiveSubstate.AWAITING_CONFIRMATION
    )
