"""US2-4: "offer reusing my last submission's answers."

sanitize_for_autofill below is a TEMPORARY stand-in for #105 ("BE - Create:
shared autofill service used by chat AND any remaining static forms",
US2-5) -- a Go service both the chatbot and any static form screen will
call so the two channels can never drift apart on what's safe to reuse
(that's literally what US2-5's acceptance criteria requires). Once #105
exists, service.py's callers should call that instead of this module's
sanitize_for_autofill; build_answer_rows (turning a sanitized dict into
real ConversationAnswer rows) stays regardless, since that part is
chatbot-specific either way.

Raw data source: Go's GET /service/tasks/last-answer (src/tasks/client.py's
fetch_last_answer, #100) -- the most recent COMPLETED form.response.answer
for (farmer, handler), completely unfiltered.
"""

from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.conversation.constants import AnswerSource
from src.conversation.models import ConversationAnswer

if TYPE_CHECKING:
    from src.conversation.service import Choice, Question

# Go's own echoed-back key (see form_handler.go's submitAnswerForUser --
# "answer" already has task_id baked in before it's stored) -- never a real
# answer, always regenerated fresh at submit time.
_NON_ANSWER_FIELDS = frozenset({"task_id"})

# service.py's own sentinel Choice ids (_SKIP_CHOICE_ID / _PAUSE_CHOICE_ID) --
# duplicated as bare strings rather than imported, since importing them at
# runtime (not just TYPE_CHECKING) would need service.py -> reuse.py ->
# service.py to resolve, and service.py is the one that will import
# start_conversation_with_autofill FROM this module. Question.choices always
# has pause prepended (and skip too, for non-mandatory) -- see service.py's
# _choices_for -- so these must be stripped before treating a Question's
# choices as "the real, storable values for this field."
_SENTINEL_CHOICE_IDS = frozenset({"__skip__", "__pause__"})


def _real_choices(question: "Question") -> list["Choice"]:
    if not question.has_constrained_choices or not question.choices:
        return []
    return [c for c in question.choices if c.id not in _SENTINEL_CHOICE_IDS]


# The 3 previously-blocked handlers' parent fields (src/line/parent_picker.py's
# FIELD_NAME values). Reusing one blind would silently attach a NEW
# submission to an OLD parent row -- a different farm activity/harvest/batch
# than the one this submission is actually about -- a data-integrity bug a
# farmer would never notice from the chat UI alone, since nothing displays
# the raw UUID. Hardcoded rather than imported from parent_picker to avoid a
# needless coupling (parent_picker has no reason to know about reuse); these
# 3 names are already stable/established elsewhere (V13's migration
# comment, parent_picker.py itself).
_STALE_PARENT_FIELDS = frozenset({"farm_activity_id", "harvest_id", "batch_id"})


def sanitize_for_autofill(
    raw_answer: dict[str, Any], questions: list["Question"]
) -> dict[str, Any]:
    """TEMPORARY -- see module docstring. Three rules:

    1. task_id and the 3 parent-id fields are dropped outright, always --
       never safe to reuse regardless of what the current form looks like.
    2. An OPTION/BOOLEAN field whose stored value doesn't match any of the
       CURRENT form's real choices for that field name is dropped -- a
       farm/fertilizer/etc. that's since been deleted or renamed shouldn't
       silently offer a UUID that no longer resolves to anything real.
    3. Every other field (free text, or an OPTION value that still
       resolves) passes through unchanged.
    """
    choices_by_field = {
        q.field_name: _real_choices(q) for q in questions if q.has_constrained_choices
    }
    sanitized: dict[str, Any] = {}
    for field_name, value in raw_answer.items():
        if field_name in _NON_ANSWER_FIELDS or field_name in _STALE_PARENT_FIELDS:
            continue
        real_choices = choices_by_field.get(field_name)
        if real_choices is not None and not any(c.id == str(value) for c in real_choices):
            continue
        sanitized[field_name] = value
    return sanitized


def build_answer_rows(
    conversation_id: UUID, sanitized_answer: dict[str, Any], questions: list["Question"]
) -> list[ConversationAnswer]:
    """Turns a sanitized reuse-answer dict into real ConversationAnswer rows
    -- the exact {"text": ..., "value": ...} shape handle_answer already
    writes for a freshly-typed answer, so confirm_conversation, the
    confirmation summary, and the edit flow all treat a prefilled field
    exactly like one the farmer just answered, no special-casing needed
    anywhere downstream of this function.

    source=GUIDED_FLOW, not a new "autofilled" value -- AnswerSource has no
    such value yet (would need its own migration), and the answer's actual
    provenance genuinely IS the guided flow, just prefilled rather than
    typed this turn. Worth reconsidering once #105 replaces this module.
    """
    question_by_field = {q.field_name: q for q in questions}
    rows = []
    for field_name, value in sanitized_answer.items():
        question = question_by_field.get(field_name)
        if question is None:
            continue  # a field name Go echoed back that isn't a real question anymore
        answer: dict[str, Any] = {"text": str(value)}
        if question.has_constrained_choices:
            # OPTION/BOOLEAN -- value is already the resolved id (matched
            # against real choices in sanitize_for_autofill above); the
            # human-readable label is what a farmer actually reviews at
            # confirmation (_format_answered_lines reads answer["text"]).
            label = next(
                (c.label for c in _real_choices(question) if c.id == str(value)),
                str(value),
            )
            answer = {"text": label, "value": str(value)}
        rows.append(
            ConversationAnswer(
                conversation_id=conversation_id,
                question_id=question.question_id,
                answer=answer,
                source=AnswerSource.GUIDED_FLOW,
            )
        )
    return rows
