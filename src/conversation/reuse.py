"""US2-4: "offer reusing my last submission's answers."

sanitize_for_autofill delegates to #105 ("BE - Create: shared autofill
service used by chat AND any remaining static forms", US2-5) --
mobile-backend's POST /service/autofill/sanitize
(internal/validation/autofill_sanitizer.go). That's the ONE shared
implementation of what's safe to reuse from a farmer's last submission, so
chat and any remaining static form screen can't drift apart on the rules.
This module only builds the request payload and turns the result into real
ConversationAnswer rows (build_answer_rows) -- chatbot-specific concerns
that have nowhere else to live.

Raw data source: Go's GET /service/tasks/last-answer (src/tasks/client.py's
fetch_last_answer, #100) -- the most recent COMPLETED form.response.answer
for (farmer, handler), completely unfiltered.
"""

from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.conversation.constants import AnswerSource
from src.conversation.models import ConversationAnswer
from src.tasks.client import fetch_sanitized_autofill

if TYPE_CHECKING:
    from src.conversation.service import Choice, Question

# service.py's own sentinel Choice ids (_SKIP_CHOICE_ID / _PAUSE_CHOICE_ID) --
# duplicated as bare strings rather than imported, since importing them at
# runtime (not just TYPE_CHECKING) would need service.py -> reuse.py ->
# service.py to resolve, and service.py is the one that will import
# start_conversation_with_autofill FROM this module. Question.choices always
# has pause prepended (and skip too, for non-mandatory) -- see service.py's
# _choices_for -- so these must be stripped before treating a Question's
# choices as "the real, storable values for this field." Purely a LINE/
# guided-flow UI concept, so it's stripped here rather than sent to Go --
# a static-form caller would never have these ids in the first place.
_SENTINEL_CHOICE_IDS = frozenset({"__skip__", "__pause__"})


def _real_choices(question: "Question") -> list["Choice"]:
    if not question.has_constrained_choices or not question.choices:
        return []
    return [c for c in question.choices if c.id not in _SENTINEL_CHOICE_IDS]


async def sanitize_for_autofill(
    raw_answer: dict[str, Any], questions: list["Question"]
) -> dict[str, Any]:
    """Builds the request payload for #105 and returns its filtered result.

    `questions` is converted to mobile-backend's internal/validation.Question
    JSON shape (fieldName/inputType/choices, choices as {id, name}) --
    Go applies the actual rules (drop task_id and the 3 stale parent-id
    fields; drop an OPTION/BOOLEAN value that no longer matches a real
    choice on the current form; pass everything else through unchanged).
    """
    questions_payload = [
        {
            "fieldName": q.field_name,
            "inputType": q.input_type,
            "choices": [{"id": c.id, "name": c.label} for c in _real_choices(q)],
        }
        for q in questions
    ]
    return await fetch_sanitized_autofill(answer=raw_answer, questions=questions_payload)


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
