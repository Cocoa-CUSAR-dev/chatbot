"""The actual state machine from target-architecture.md #4 -- transition
rules only, no I/O. Callers (src/line's event dispatcher) are responsible for
actually calling the LLM client / task client / etc. and persisting the
result; this module just decides what should happen next.

Two rules this exists specifically to enforce (both were bugs in earlier
drafts of this design -- see ADR 0004's amendment):
  1. "Slots still missing" alone never triggers GuidedFlow. Only genuine LLM
     failure/timeout does. See `on_extraction_result`.
  2. The switch between LLMConversation and GuidedFlow is bidirectional and
     can happen mid-loop, not just once. See `on_llm_recovered` and
     `on_llm_failed`.
"""

from dataclasses import dataclass

from src.conversation.constants import ActiveSubstate


@dataclass(frozen=True)
class Transition:
    next_state: ActiveSubstate
    reason: str


def on_extraction_result(*, all_slots_filled: bool) -> Transition:
    """Called after LLMConversation.Extracting gets a successful response
    (not a failure -- see on_llm_failed for that case).
    """
    if all_slots_filled:
        return Transition(ActiveSubstate.AWAITING_CONFIRMATION, "all required slots filled")
    return Transition(
        ActiveSubstate.LLM_ASKING_FOLLOW_UP,
        "slots still missing, LLM still healthy -- NOT a fallback trigger",
    )


def on_llm_failed() -> Transition:
    """The ONLY trigger for entering GuidedFlow. Can fire from either
    LLM_EXTRACTING or LLM_ASKING_FOLLOW_UP -- see the state diagram's note
    that this transition is drawn at the composite level on purpose.
    """
    return Transition(
        ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION,
        "LLM failed/errored/past time budget",
    )


def on_llm_recovered() -> Transition:
    """The reverse of on_llm_failed -- can fire from either GuidedFlow
    substate, same "composite-level transition" reasoning.
    """
    return Transition(
        ActiveSubstate.LLM_ASKING_FOLLOW_UP,
        "LLM available again, or farmer answered with free text",
    )


def on_guided_answer(*, all_slots_filled: bool) -> Transition:
    if all_slots_filled:
        return Transition(ActiveSubstate.AWAITING_CONFIRMATION, "all required slots filled")
    return Transition(ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION, "answer stored, slots remain")
