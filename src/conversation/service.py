"""Orchestrates a conversation's GuidedFlow lifecycle -- Sprint 1 scope only,
no LLM (see target-architecture.md #4). Pure decision logic lives in
state_machine.py; this module is the I/O layer around it: loads/persists
Conversation + ConversationAnswer rows and decides what to say back.

Deliberately takes the form's question script (`FormDetail`) as a parameter
rather than fetching it itself -- lets the same logic run against either
`forms.client.get_form()` (real Kotlin call, production) or a direct DB query
(this sprint's test router, see src/conversation/router.py) without this
module knowing or caring which. Confirmed against Kotlin's actual DTOs
(web-backend's FormRepository/Question.kt): each question dict carries
question_id/label/field_name/is_mandatory/sort_order, and OPTION-type
questions additionally carry choices: [{id, name}].

OPTION-question answers: a farmer picks by the choice's label (matches a
LINE Quick Reply MessageAction, which sends its own label back as the
message text -- see src/line/service.py's QuickReplyOption). handle_answer
resolves that label against the open question's own choice list and stores
the resolved id as answer["value"] (falling back to answer["text"] alone for
non-OPTION questions, which have no choices to resolve against). If the
farmer's text doesn't match any listed choice, the question is re-asked
rather than silently accepting text that can't be stored as a real domain
value -- this is exactly what caused a real failed submission before choices
existed here (see this task's own history: guided-flow text answers for
OPTION questions failed with `invalid input syntax for type uuid`).
"""

import logging
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from src.conversation import llm_parsing, reuse, text_parsing
from src.conversation.constants import (
    PAUSE_LABEL,
    ActiveSubstate,
    AnswerSource,
    ConversationStatus,
)
from src.conversation.exceptions import ConversationNotFound
from src.conversation.models import Conversation, ConversationAnswer
from src.conversation.state_machine import on_guided_answer
from src.conversation.validation import validate_answer
from src.forms.schemas import FormDetail
from src.line import parent_picker, plot_picker
from src.tasks.client import submit_task
from src.tasks.exceptions import HandlerNotSupported
from src.tasks.schemas import TaskSubmission

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Choice:
    id: str
    label: str


# BOOLEAN questions target a real `boolean` column (e.g.
# agriculture.farm_pest_disease_record.is_quality_damage) -- free text like
# "ไม่"/"aaa" fails with `invalid input syntax for type boolean`, the exact
# same failure OPTION questions had before choices existed. Kotlin never
# sends a `choices` list for BOOLEAN (its own filter is INPUT_TYPE ==
# OPTION only), so these two are synthesized here instead of looked up --
# same choices/resolution/re-ask mechanism either way, just a fixed pair
# instead of a database-backed list.
_BOOLEAN_CHOICES = [Choice(id="true", label="ใช่"), Choice(id="false", label="ไม่")]

# Offered only on non-mandatory questions, always first when real choices
# exist too -- elderly farmers are the primary users here, so "this one's
# optional" needs to be visually obvious, not just implied by phrasing.
# LINE Quick Reply buttons can't be recolored (all MessageAction pills share
# one style), so an icon prefix is the actual lever available -- id "__skip__"
# is a sentinel, never a real domain value.
_SKIP_CHOICE_ID = "__skip__"
_SKIP_CHOICE = Choice(id=_SKIP_CHOICE_ID, label="⏭️ ข้าม")

# Offered on EVERY guided-flow question, mandatory or not (US2-3: "farmer
# pause at anytime") -- unlike skip, this never affects has_constrained_choices,
# since it's not an answer at all: handle_answer intercepts it before any
# answer-storage logic runs, leaving current_question_id and every already-
# given answer exactly as they are. Same sentinel-id pattern as skip.
_PAUSE_CHOICE_ID = "__pause__"
_PAUSE_CHOICE = Choice(id=_PAUSE_CHOICE_ID, label=PAUSE_LABEL)

# LINE's own hard cap on Quick Reply buttons per message -- some OPTION
# questions already carry this many real choices, so prepending skip must
# make room for it rather than pushing the last real choice off the list.
_QUICK_REPLY_LIMIT = 13

# Pagination (the mandatory-OPTION regression PR #25's review caught was
# exactly this problem, one slot short: real choices overflowing what's left
# after pause/skip -- _choices_for's slicing below just drops the overflow
# silently. These two sentinel choices, same pattern as pause/skip, let a
# farmer actually reach the rest instead of losing it.
_NEXT_PAGE_CHOICE_ID = "__next_page__"
_NEXT_PAGE_CHOICE = Choice(id=_NEXT_PAGE_CHOICE_ID, label="» ถัดไป")
_PREV_PAGE_CHOICE_ID = "__prev_page__"
_PREV_PAGE_CHOICE = Choice(id=_PREV_PAGE_CHOICE_ID, label="« ก่อนหน้า")

# Reserved together, only once a question's real choices don't fit in one
# page at all alongside pause(/skip) -- see _paginate. Both slots exist on
# EVERY page of a paginated question regardless of position (even page 1,
# where prev is unused, or the last page, where next is unused), trading a
# little button density for keeping the slot math trivially safe: the worst
# case is always exactly _QUICK_REPLY_LIMIT total, never "depends which
# page." Deliberately not repeating the review-caught mistake with
# per-page-conditional sizing.
_PAGE_NAV_RESERVED = 2


@dataclass(frozen=True)
class Question:
    question_id: UUID
    label: str
    field_name: str
    input_type: str
    is_mandatory: bool
    sort_order: int
    choices: list[Choice] | None = None
    # True only for real constrained choices (OPTION/BOOLEAN) -- distinct
    # from "has a skip button but is otherwise free text", which must still
    # accept arbitrary typed text rather than re-asking on a non-match.
    has_constrained_choices: bool = False
    # From Kotlin's form.field_validation_rule, already joined onto this
    # question server-side (FormRepository.kt) -- null for OPTION/BOOLEAN/
    # upload (constrained another way already) or any field_name with no
    # rule row. See validation.py's own docstring for the key-casing wrinkle.
    validation_rule: dict[str, Any] | None = None
    # The full, un-paginated real choice list (OPTION/BOOLEAN only, no
    # pause/skip/nav prepended, no slicing) -- None for free text. _paginate
    # slices this per page; `choices` above stays the page-0 rendering,
    # unchanged, for every question that never needs more than one page
    # (the vast majority) -- those call sites don't need to change at all.
    all_real_choices: list[Choice] | None = None
    # form.question.carry_forward (V21). When the farmer taps "➕ เพิ่มอีกรายการ"
    # on a multi-submit form, start_next_submission copies this question's
    # answer from the row just submitted instead of asking it again -- e.g.
    # the plot on a farm_activity form, where only the activity changes.
    carry_forward: bool = False


@dataclass(frozen=True)
class ConversationReply:
    conversation_id: UUID
    substate: ActiveSubstate
    text: str
    choices: list[Choice] | None = None
    # True only when confirm_conversation's own submit_task call failed --
    # lets the caller re-attach the confirm button for a retry instead of
    # treating this as the terminal "saved" message.
    submission_failed: bool = False
    # True only on a SUCCESSFUL confirm of a multiple-submit form: the
    # submission saved, but this task expects more rows (three grades for
    # one harvest), so the caller offers "add another" instead of the
    # terminal thanks. Same "let the caller attach the right buttons"
    # pattern as submission_failed above.
    offer_another: bool = False
    # True when confirm_conversation submitted NOTHING because the tap
    # wasn't a real confirmation any more: a second tap after the first one
    # already saved everything, or an old ยืนยัน button pressed while a
    # question is open again (mid-edit, mid-selection). The caller renders
    # it as an ordinary step -- no quick ack, no diary -- since nothing was
    # saved by this request.
    nothing_submitted: bool = False
    # Debug-only passthrough of the open question's own shape -- None
    # whenever this reply isn't "here's a fixed question to answer" (e.g.
    # confirmation/completed/cancelled replies have no single open question).
    # Not used by the real LINE webhook path; exists so the dev test UI can
    # show a developer what's actually being validated without guessing.
    input_type: str | None = None
    validation_rule: dict[str, Any] | None = None
    # Multi-choice picker: an OPTION question on a multiple-submit form, asked
    # as a Flex bubble the farmer can tick several answers on, each becoming
    # its own saved row. When set, the caller renders the bubble instead of
    # Quick Reply buttons, and `choices` stays None -- the picker's options
    # are not always the question's own (plot_id uses the farmer's scoped
    # plots, not Kotlin's global ref.plot_constant list).
    multi_choice: bool = False
    picker_options: list[Choice] | None = None
    # Already-ticked options, so a bubble re-sent after a pause/resume or an
    # edit shows what the farmer picked before rather than starting blank.
    selected_ids: frozenset[str] = frozenset()
    # The picker's skip button label, or None when the question is mandatory
    # -- the same rule as the Quick Reply skip button. "ทั้งฟาร์ม" on the plot
    # question (skipping it there means the whole farm), "⏭️ ข้าม" elsewhere.
    skip_label: str | None = None
    # True for the short "เลือกแล้ว: ..." acknowledgement after a tap, which
    # is a text line with one "เสร็จ" button rather than a new bubble.
    selection_ack: bool = False


def _constrained_choices_for(q: dict[str, Any]) -> list[Choice] | None:
    if q.get("choices"):
        return [Choice(id=str(c["id"]), label=str(c.get("name") or "")) for c in q["choices"]]
    if q.get("input_type") == "BOOLEAN":
        return _BOOLEAN_CHOICES
    return None


def _choices_for(q: dict[str, Any], is_mandatory: bool) -> tuple[list[Choice], bool]:
    """Returns (choices to offer, whether they're a real constrained set).

    Pause is prepended to every question regardless of mandatory/constrained
    status -- it's never itself a constrained choice (see _PAUSE_CHOICE_ID).
    Beyond that: mandatory questions get exactly the underlying OPTION/
    BOOLEAN choices, or none, for free text. Non-mandatory questions always
    also get a skip button; a non-mandatory free-text question gets a
    skip+pause-only Quick Reply, but typing real text is still accepted
    (has_constrained_choices stays False).
    """
    constrained = _constrained_choices_for(q)
    if is_mandatory:
        if constrained is not None:
            # Reviewer-caught regression: this branch used to return
            # `constrained` completely unsliced, on the assumption a
            # mandatory question's real choices were already within LINE's
            # 13-item cap. Adding pause broke that assumption -- a
            # mandatory OPTION field with exactly 13 real choices (e.g.
            # farm_id) became 14 total, and router.py's own send-time
            # `choices[:13]` slice silently dropped the LAST real choice --
            # a farm a farmer could no longer actually select via button,
            # not just a cosmetic overflow. Same room-for-pause treatment
            # the non-mandatory branch below already had.
            room_for_real_choices = _QUICK_REPLY_LIMIT - 1
            return [_PAUSE_CHOICE, *constrained[:room_for_real_choices]], True
        return [_PAUSE_CHOICE], False
    if constrained is not None:
        room_for_real_choices = _QUICK_REPLY_LIMIT - 2  # room for both skip and pause
        return [_SKIP_CHOICE, _PAUSE_CHOICE, *constrained[:room_for_real_choices]], True
    return [_SKIP_CHOICE, _PAUSE_CHOICE], False


# DATE/DATETIME/FLOAT/INT all validate as free text through the same
# validate_answer()-then-re-ask path VARCHAR/BOOLEAN already use, with no
# LINE-side UI dependency -- CB-9 confirmed there's no blocker for any of
# them. GEODATA stays deferred: it needs a storage.geo row + FK link, which
# Go's dissection (SubmitTaskForUser, form_handler.go) still only does as a
# single-table flat insert with no storage.geo handling at all -- a farmer
# could answer a GEODATA question here and have the submission silently
# lose the coordinate. "upload" is a VARCHAR field_name convention (see
# form.question seed data) for photo attachments, which have nowhere to go
# yet either. Filtered out at the form level (not just skipped when picking
# the next question) so unsupported types never show up in the guided flow
# OR the confirmation summary.
_SUPPORTED_INPUT_TYPES = {"VARCHAR", "OPTION", "BOOLEAN", "FLOAT", "INT", "DATE", "DATETIME"}


def _is_supported(q: dict[str, Any]) -> bool:
    if q.get("input_type") not in _SUPPORTED_INPUT_TYPES:
        return False
    return not (q.get("input_type") == "VARCHAR" and q.get("field_name") == "upload")


def _question_from_dict(q: dict[str, Any]) -> Question:
    is_mandatory = bool(q.get("is_mandatory", False))
    choices, has_constrained_choices = _choices_for(q, is_mandatory)
    return Question(
        question_id=UUID(str(q["question_id"])),
        label=str(q.get("label") or ""),
        field_name=str(q.get("field_name") or ""),
        input_type=str(q.get("input_type") or ""),
        is_mandatory=is_mandatory,
        sort_order=int(q.get("sort_order", 0)),
        choices=choices,
        has_constrained_choices=has_constrained_choices,
        validation_rule=q.get("validation_rule"),
        all_real_choices=_constrained_choices_for(q),
        carry_forward=bool(q.get("carry_forward", False)),
    )


def questions_from_form(form: FormDetail) -> list[Question]:
    """Flattens a FormDetail's sections into a sort_order-ordered list."""
    questions = [
        _question_from_dict(q)
        for section in form.sections
        for q in section.get("questions", [])
        if _is_supported(q)
    ]
    return sorted(questions, key=lambda q: q.sort_order)


def _next_unanswered_required(questions: list[Question], answered: set[UUID]) -> Question | None:
    # Deliberately ignores is_mandatory -- every question gets asked, not
    # just required ones. Started as a workaround for the dev DB's
    # inconsistent is_mandatory data, but is now load-bearing: a
    # non-mandatory question only ever reaches the farmer (with its skip
    # button, see _choices_for) if this function selects it as "next".
    # Filtering on is_mandatory here would make skip buttons unreachable.
    for question in questions:
        if question.question_id not in answered:
            return question
    return None


def _all_required_answered(questions: list[Question], answered: set[UUID]) -> bool:
    return _next_unanswered_required(questions, answered) is None


def _answered_question_ids(answer_rows: list[ConversationAnswer]) -> set[UUID]:
    """Questions that actually have an answer.

    A multi-choice selection in progress writes its row early (that is
    where the running selection lives, so it survives a pause), but it is NOT
    an answer yet -- counting it would let the farmer reach the confirmation
    summary with a half-made selection, or skip the question entirely by
    tapping one option and walking away.
    """
    return {row.question_id for row in answer_rows if not row.answer.get("selecting")}


async def _answered_rows(
    session: AsyncSession, conversation_id: UUID, *, refresh: bool = False
) -> list[ConversationAnswer]:
    """refresh=True re-reads rows this session may already hold (see
    confirm_conversation: after waiting on a lock, a stale in-memory
    `submitted` would re-send rows that another request already saved).
    """
    statement = select(ConversationAnswer).where(
        ConversationAnswer.conversation_id == conversation_id
    )
    if refresh:
        statement = statement.execution_options(populate_existing=True)
    result = await session.execute(statement)
    return list(result.scalars().all())


def _format_answered_lines(questions: list[Question], answers: list[ConversationAnswer]) -> str:
    """Shared by the confirmation summary and the resume recap (US2-3) --
    both need "label: answer" per already-answered question, just with
    different framing text around them.
    """
    label_by_id = {q.question_id: q.label for q in questions}
    sort_order_by_id = {q.question_id: q.sort_order for q in questions}
    # Skipped fields were saved as nothing -- leave them out of the review
    # entirely rather than showing a confusing "skipped" line. An unfinished
    # multi-choice selection is left out for the same reason: it has no answer
    # text yet, only a running list of taps.
    answered = [a for a in answers if not a.answer.get("skipped") and not a.answer.get("selecting")]
    ordered = sorted(answered, key=lambda a: sort_order_by_id.get(a.question_id, 0))
    # Always the human-readable text, even for resolved OPTION answers --
    # farmers review labels ("พ่นยา"), not the underlying UUID.
    lines = [f"- {label_by_id.get(a.question_id, '?')}: {a.answer.get('text')}" for a in ordered]
    return "\n".join(lines)


def _format_confirmation_summary(
    questions: list[Question], answers: list[ConversationAnswer]
) -> str:
    lines = _format_answered_lines(questions, answers)
    summary = "สรุปคำตอบของคุณ:\n" + lines

    # One extra line for a multi-choice answer. Its own line already reads
    # "แปลง A, แปลง C" (it is just answer["text"]), which does not tell the
    # farmer that confirming is about to create several separate records --
    # and that is the one consequence of this feature they should not
    # discover afterwards.
    multi_row = _multi_choice_row(answers)
    if multi_row is not None:
        labels = [str(label) for label in multi_row.answer.get("labels", [])]
        question_label = next(
            (q.label for q in questions if q.question_id == multi_row.question_id), ""
        )
        summary += (
            f"\n\nจะบันทึกเป็น {len(labels)} รายการ (แยกตาม{question_label}): {', '.join(labels)}"
        )

    return summary + "\n\nยืนยันการส่งข้อมูลหรือไม่?"


def _format_resume_recap(questions: list[Question], answers: list[ConversationAnswer]) -> str:
    """Prefix for a resumed conversation's first message back (US2-3): the
    questions already answered, so the farmer isn't left guessing what they
    already told the bot before picking back up. Empty when there's nothing
    answered yet (e.g. paused right at the parent-picker step) -- no point
    prefixing an empty recap section.
    """
    lines = _format_answered_lines(questions, answers)
    if not lines:
        return ""
    return "คำตอบที่บันทึกไว้:\n" + lines + "\n\n"


def _leading_choices_for(question: Question) -> list[Choice]:
    """Same pause(/skip) prefix _choices_for already computes -- re-derived
    here from is_mandatory rather than threading it through separately,
    since Question already carries it.
    """
    if question.is_mandatory:
        return [_PAUSE_CHOICE]
    return [_SKIP_CHOICE, _PAUSE_CHOICE]


@dataclass(frozen=True)
class _Page:
    choices: list[Choice] | None
    # "" for every non-paginated question (the vast majority) -- unchanged
    # label text. " (หน้า 2/3)" once a question actually spans multiple
    # pages, so a farmer knows there's more to see beyond this message.
    indicator: str


def _paginate(question: Question, page: int) -> _Page:
    """Slices a question's real choices to whichever page the farmer is
    currently viewing (conversation.current_page), re-prepending pause(/
    skip) and appending prev/next nav choices only when actually needed.

    The vast majority of questions never hit the paginated branch at all:
    if every real choice already fits in one message alongside pause(/skip)
    -- exactly what _choices_for already computed -- this returns
    question.choices completely unchanged, `page` is irrelevant. Only once
    real choices exceed that does this re-slice using the smaller,
    nav-reserving budget, uniformly across every page of that question (see
    _PAGE_NAV_RESERVED) rather than trying to reclaim the unused slot on
    the first/last page -- deliberately simpler and safer than optimizing
    density, given review already caught one off-by-one in this exact area.
    """
    if question.all_real_choices is None:
        return _Page(choices=question.choices, indicator="")

    leading = _leading_choices_for(question)
    single_page_budget = _QUICK_REPLY_LIMIT - len(leading)
    if len(question.all_real_choices) <= single_page_budget:
        return _Page(choices=question.choices, indicator="")

    page_budget = single_page_budget - _PAGE_NAV_RESERVED
    total_pages = -(-len(question.all_real_choices) // page_budget)  # ceil division
    page = max(0, min(page, total_pages - 1))  # defensively clamp a stale/bad value
    start = page * page_budget
    page_real_choices = question.all_real_choices[start : start + page_budget]

    nav: list[Choice] = []
    if page > 0:
        nav.append(_PREV_PAGE_CHOICE)
    if page < total_pages - 1:
        nav.append(_NEXT_PAGE_CHOICE)

    return _Page(
        choices=[*leading, *page_real_choices, *nav],
        indicator=f" (หน้า {page + 1}/{total_pages})",
    )


def _advance_to(conversation: Conversation, question_id: UUID | None) -> None:
    """The only place current_question_id should ever be assigned outside
    object construction -- always resets current_page alongside it, since a
    page number only ever means something relative to whichever question is
    currently open. Doesn't apply to next/prev-page handling itself, which
    changes current_page WITHOUT touching current_question_id at all --
    that's the whole point of paging within one question.
    """
    conversation.current_question_id = question_id
    conversation.current_page = 0


# The plot question is the one OPTION question whose options the picker does
# NOT take from the question itself: Kotlin sends it every plot in the system
# (ref.plot_constant), so it is re-scoped to the farmer's own plots. Both are
# the handler's own column names (see Go's dissectAnswer), not display text.
PLOT_FIELD_NAME = "plot_id"
_FARM_FIELD_NAME = "farm_id"
# Skipping the plot question has always meant "the whole farm" (the form says
# "หากทำทั้งฟาร์มไม่ต้องระบุ"), so the picker says so in words; every other
# optional question keeps the ordinary skip label.
_WHOLE_FARM_LABEL = "ทั้งฟาร์ม"


def is_multi_choice_question(question: Question, form: FormDetail) -> bool:
    """The cheap half of "should this question be asked as a multi-choice
    picker" -- the half that needs no database.

    Both conditions are necessary. OPTION, because ticking several answers
    only makes sense where the answers are a fixed list (BOOLEAN is a single
    yes/no by nature, and free text has nothing to tick). And
    is_multiple_submit, because the fan-out writes one row per ticked answer:
    on a form the researchers did NOT mark multiple-submit, that is precisely
    the duplicate they said they didn't want. Every other question, and every
    question on an ordinary form, keeps today's Quick Reply.

    The remaining conditions need a query, so they live in
    multi_choice_options_for below. Callers should use that one.
    """
    return form.is_multiple_submit and question.input_type == "OPTION"


async def multi_choice_options_for(
    session: AsyncSession,
    *,
    conversation: Conversation,
    question: Question,
    form: FormDetail,
) -> list[Choice] | None:
    """The options to offer as a multi-choice picker for `question`, or None
    to ask it the ordinary Quick Reply way.

    One function so every place that can ask a question -- a fresh start, an
    answer advancing to the next one, a resume, an edit -- agrees on whether
    the farmer sees a bubble or buttons.

    None when:
    - the question isn't eligible at all (is_multi_choice_question), or
    - ANOTHER question in this conversation already holds several answers.
      One multi-answer question per submission is a deliberate rule: the
      number of rows written is then simply the number ticked on that one
      question. Allowing two would mean either a cross product (2 plots x 2
      activities = 4 rows, easy to create by accident) or an ambiguity about
      which question the rows are split by. So once one question fans out,
      every other question is answered once, the ordinary way.
    - there are fewer than 2 options: a picker for one option is strictly
      worse than today's button, and an empty one would be a dead end.
    """
    if not is_multi_choice_question(question, form):
        return None

    answer_rows = await _answered_rows(session, conversation.conversation_id)
    if any(
        row.answer.get("multi") and row.question_id != question.question_id for row in answer_rows
    ):
        return None

    options: list[Choice]
    if question.field_name == PLOT_FIELD_NAME:
        plots = await plot_picker.list_plots(
            session,
            conversation.user_id,
            farm_id=_answered_farm_id(answer_rows, form=form),
        )
        options = [Choice(id=plot.id, label=plot.label) for plot in plots]
    else:
        # The full un-paginated list: the bubble/carousel replaces the 13-a-
        # page Quick Reply pagination for these questions.
        options = list(question.all_real_choices or [])

    if len(options) < 2:
        return None
    return options


def _answered_farm_id(answer_rows: list[ConversationAnswer], *, form: FormDetail) -> str | None:
    """The farm the farmer already picked in THIS conversation, if the form
    asks for one before the plot question -- plots from their other farms
    would contradict an answer they just gave. None when the form has no farm
    question (the "จดกิจกรรมในสวน" case), it isn't answered yet, or it was
    answered with several farms (then no single farm narrows the plots).
    """
    farm_question_ids = {
        q.question_id for q in questions_from_form(form) if q.field_name == _FARM_FIELD_NAME
    }
    if not farm_question_ids:
        return None
    for row in answer_rows:
        if row.question_id in farm_question_ids and not row.answer.get("skipped"):
            value = row.answer.get("value")
            return str(value) if value else None
    return None


# What a farmer can type instead of tapping "✅ เสร็จ" on the bubble. Deliberately
# short and exact: anything longer is far more likely to be an option's label
# than a command.
_DONE_WORDS = frozenset({"เสร็จ", "เสร็จแล้ว", "จบ", "done", "ok"})
# Sentinel id, never a real choice id -- lets the typed-text path run the skip
# button's label through the same match_choice call as the real options.
_PICKER_SKIP_ID = "__picker_skip__"
# Tapping a bubble that has already been answered. Bubbles stay tappable
# forever (LINE can't retract or edit a sent message), so this is a normal
# thing for a farmer to do, not an error -- it says so plainly and changes
# nothing.
_STALE_PICKER_TEXT = "คำถามนี้ผ่านไปแล้วครับ"


def _skip_label_for(question: Question) -> str | None:
    if question.is_mandatory:
        return None
    if question.field_name == PLOT_FIELD_NAME:
        return _WHOLE_FARM_LABEL
    return _SKIP_CHOICE.label


def _multi_choice_reply(
    conversation_id: UUID,
    question: Question,
    options: list[Choice],
    *,
    selected: frozenset[str] = frozenset(),
    prefix: str = "",
    error: str | None = None,
) -> ConversationReply:
    """The picker bubble as a ConversationReply. `choices` stays None on
    purpose: for the plot question the picker's options are NOT the
    question's own (those are Kotlin's global plot list), and rendering both
    would offer two different lists in one message.
    """
    label = question.label
    text = f"{error}\n\n{label}" if error else prefix + label
    return ConversationReply(
        conversation_id=conversation_id,
        substate=ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION,
        text=text,
        multi_choice=True,
        picker_options=options,
        selected_ids=selected,
        skip_label=_skip_label_for(question),
        input_type=question.input_type,
        validation_rule=question.validation_rule,
    )


async def _ask_question(
    session: AsyncSession,
    *,
    conversation: Conversation,
    question: Question,
    form: FormDetail,
    prefix: str = "",
) -> ConversationReply:
    """Ask `question`, as a multi-choice bubble when it is one and as today's
    Quick Reply question otherwise.

    Every path that can put a question in front of a farmer goes through here
    (fresh start, autofill start, resume, edit, add-another, advancing after
    an answer) so none of them can disagree about which rendering a question
    gets -- a farmer who paused mid-selection and resumed into plain Quick
    Reply buttons would lose the selection they could no longer see.
    """
    options = await multi_choice_options_for(
        session, conversation=conversation, question=question, form=form
    )
    if options is None:
        reply = _reply_for_question(conversation.conversation_id, question)
        return replace(reply, text=prefix + reply.text) if prefix else reply
    values, _ = await _current_selection(session, conversation)
    return _multi_choice_reply(
        conversation.conversation_id,
        question,
        options,
        selected=frozenset(values),
        prefix=prefix,
    )


async def _selection_row(
    session: AsyncSession, conversation: Conversation
) -> ConversationAnswer | None:
    """The answer row for the question currently open, if there is one.

    While a selection is being built this row exists and carries
    {"selecting": true, ...} -- that is what makes a selection survive a
    pause, a resume, or the farmer simply walking away mid-tap.
    """
    if conversation.current_question_id is None:
        return None
    rows = await _answered_rows(session, conversation.conversation_id)
    return next((row for row in rows if row.question_id == conversation.current_question_id), None)


async def _current_selection(
    session: AsyncSession, conversation: Conversation
) -> tuple[list[str], list[str]]:
    """(values, labels) already chosen on the open question.

    Reads all three shapes that row can be in: a selection in progress or a
    finished multi answer ("values"/"labels"), or an ordinary single answer
    ("value"/"text") -- the last one matters when "แก้ไข" re-opens a question
    that was answered once, so the bubble starts with that answer ticked
    rather than blank.
    """
    row = await _selection_row(session, conversation)
    if row is None or row.answer.get("skipped"):
        return [], []
    if "values" in row.answer:
        return (
            [str(v) for v in row.answer.get("values", [])],
            [str(label) for label in row.answer.get("labels", [])],
        )
    if row.answer.get("value"):
        return [str(row.answer["value"])], [str(row.answer.get("text") or "")]
    return [], []


def _selection_text(labels: list[str]) -> str:
    if not labels:
        return "ยังไม่ได้เลือก"
    return f"เลือกแล้ว: {', '.join(labels)} ({len(labels)} รายการ)"


async def _store_selection(
    session: AsyncSession,
    *,
    conversation: Conversation,
    values: list[str],
    labels: list[str],
) -> None:
    """Persists the in-progress selection on the open question's own answer
    row, leaving current_question_id where it is -- the question is still
    open, the farmer is still choosing.
    """
    answer = {"selecting": True, "values": values, "labels": labels}
    row = await _selection_row(session, conversation)
    if row is None:
        session.add(
            ConversationAnswer(
                conversation_id=conversation.conversation_id,
                question_id=conversation.current_question_id,
                answer=answer,
                source=AnswerSource.GUIDED_FLOW,
            )
        )
    else:
        # Reassign, never mutate in place -- the JSONB column isn't a
        # MutableDict, so an in-place append would never be flushed (the same
        # trap _store_answer_and_advance documents).
        row.answer = answer
    await session.commit()


def _multi_choice_answer(values: list[str], labels: list[str]) -> dict[str, Any]:
    """The finished answer for a picker selection.

    One answer is stored EXACTLY like a normal OPTION answer -- {"text",
    "value"} -- so everything downstream (the summary, the payload builder,
    autofill, carry-forward) handles the common case without knowing the
    picker exists. Only a genuine 2+ selection carries the keys that trigger
    the fan-out.
    """
    if len(values) == 1:
        return {"text": labels[0], "value": values[0]}
    return {
        "text": ", ".join(labels),
        "values": values,
        "labels": labels,
        "multi": True,
        # Which answers have actually reached Go. Appended to one at a time,
        # so a retry after a partial failure never re-sends a row that
        # already landed (see confirm_conversation).
        "submitted": [],
    }


async def _locked_selection(
    session: AsyncSession, *, conversation_id: UUID, form: FormDetail
) -> tuple[Conversation, Question, list[Choice]] | None:
    """Loads the conversation FOR UPDATE and checks that a picker button
    tapped right now is still meaningful. None means "stale, change nothing".

    The lock is what keeps two fast taps from both reading the same selection
    and one of them overwriting the other; LINE delivers them as two
    independent webhook requests. It waits rather than failing fast (unlike
    handle_answer's NOWAIT, which deliberately drops a second message): both
    taps are real selections and both should land, just one after the other.

    Stale is the normal case, not an error: a Flex bubble can never be
    retracted or edited, so yesterday's picker is still sitting in the chat
    with live buttons.
    """
    conversation = (
        await session.execute(
            select(Conversation)
            .where(Conversation.conversation_id == conversation_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if conversation is None:
        raise ConversationNotFound()
    if conversation.status != ConversationStatus.ACTIVE:
        return None

    question = next(
        (q for q in questions_from_form(form) if q.question_id == conversation.current_question_id),
        None,
    )
    if question is None:
        return None
    options = await multi_choice_options_for(
        session, conversation=conversation, question=question, form=form
    )
    if options is None:
        return None
    return conversation, question, options


def _stale_reply(conversation_id: UUID) -> ConversationReply:
    return ConversationReply(
        conversation_id=conversation_id,
        substate=ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION,
        text=_STALE_PICKER_TEXT,
    )


async def toggle_choice(
    session: AsyncSession, *, conversation_id: UUID, choice_id: str, form: FormDetail
) -> ConversationReply:
    """Adds or removes one option from the running selection (the
    "multi_toggle" postback). Replies with the running selection as a short
    line, not a new bubble -- the bubble is still on screen and tappable.
    """
    locked = await _locked_selection(session, conversation_id=conversation_id, form=form)
    if locked is None:
        return _stale_reply(conversation_id)
    conversation, _, options = locked

    option = next((o for o in options if o.id == choice_id), None)
    if option is None:
        # Not one of the options on offer right now -- e.g. a plot that is no
        # longer this farmer's, or a hand-crafted postback (postback data is
        # attacker-controllable in principle). Refuse rather than store an id
        # the fan-out would later send to Go.
        return _stale_reply(conversation_id)

    values, labels = await _current_selection(session, conversation)
    if choice_id in values:
        index = values.index(choice_id)
        values.pop(index)
        labels.pop(index)
    else:
        values.append(choice_id)
        labels.append(option.label)

    await _store_selection(session, conversation=conversation, values=values, labels=labels)
    return ConversationReply(
        conversation_id=conversation_id,
        substate=ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION,
        text=_selection_text(labels),
        selection_ack=True,
    )


async def finish_selection(
    session: AsyncSession, *, conversation_id: UUID, form: FormDetail
) -> ConversationReply:
    """Ends the selection (the "multi_done" postback / a typed "เสร็จ").

    One option stores a normal single answer, so the rest of the system never
    learns the picker was involved; 2+ store the multi answer the fan-out
    reads. Either way the conversation advances exactly like any other
    answered question, because both go through _store_answer_and_advance.
    """
    locked = await _locked_selection(session, conversation_id=conversation_id, form=form)
    if locked is None:
        return _stale_reply(conversation_id)
    conversation, question, options = locked

    values, labels = await _current_selection(session, conversation)
    if not values:
        hint = "กรุณาเลือกอย่างน้อย 1 รายการ"
        skip_label = _skip_label_for(question)
        if skip_label is not None:
            hint += f' หรือกด "{skip_label}"'
        return _multi_choice_reply(conversation_id, question, options, error=hint)

    return await _store_answer_and_advance(
        session,
        conversation=conversation,
        questions=questions_from_form(form),
        form=form,
        answer=_multi_choice_answer(values, labels),
    )


async def skip_selection(
    session: AsyncSession, *, conversation_id: UUID, form: FormDetail
) -> ConversationReply:
    """The picker's skip button ("ทั้งฟาร์ม" on the plot question, "⏭️ ข้าม"
    elsewhere) -- stored as {"skipped": true}, byte for byte what the skip
    button on today's Quick Reply version of the question stores. Anything
    ticked before is discarded.
    """
    locked = await _locked_selection(session, conversation_id=conversation_id, form=form)
    if locked is None:
        return _stale_reply(conversation_id)
    conversation, question, options = locked

    if question.is_mandatory:
        # The bubble never offers the button for a mandatory question, but a
        # stale bubble from before the form was edited still could.
        return _multi_choice_reply(
            conversation_id,
            question,
            options,
            error="คำถามนี้จำเป็นต้องเลือกอย่างน้อย 1 รายการครับ",
        )

    return await _store_answer_and_advance(
        session,
        conversation=conversation,
        questions=questions_from_form(form),
        form=form,
        answer={"skipped": True},
    )


async def _handle_typed_selection_text(
    session: AsyncSession,
    *,
    conversation: Conversation,
    question: Question,
    options: list[Choice],
    form: FormDetail,
    raw_text: str,
) -> ConversationReply:
    """Typing while the picker is open.

    A farmer who types instead of tapping must never hit a dead end
    (docs-and-plan#189's rule): an option's label toggles it, "เสร็จ" ends the
    selection, the skip label skips, and anything else re-sends the bubble
    with a nudge rather than being stored -- storing it would put free text
    where a choice id belongs.
    """
    text = raw_text.strip()
    if text.casefold() in _DONE_WORDS:
        return await finish_selection(
            session, conversation_id=conversation.conversation_id, form=form
        )

    # match_choice, not a bare label comparison: it already forgives a
    # trailing politeness particle ("แปลง A ครับ"), and reusing it keeps typed
    # labels behaving like every other typed choice in this flow. The skip
    # label rides along in the same list so it gets the same forgiveness.
    candidates = list(options)
    skip_label = _skip_label_for(question)
    if skip_label is not None:
        candidates.append(Choice(id=_PICKER_SKIP_ID, label=skip_label))

    matched = text_parsing.match_choice(text, candidates)
    if matched is not None and matched.id == _PICKER_SKIP_ID:
        return await skip_selection(
            session, conversation_id=conversation.conversation_id, form=form
        )
    if matched is not None:
        return await toggle_choice(
            session,
            conversation_id=conversation.conversation_id,
            choice_id=matched.id,
            form=form,
        )

    values, _ = await _current_selection(session, conversation)
    return _multi_choice_reply(
        conversation.conversation_id,
        question,
        options,
        selected=frozenset(values),
        error="กรุณากดเลือกจากปุ่มด้านบนครับ",
    )


def _reply_for_question(
    conversation_id: UUID, question: Question, *, page: int = 0, error: str | None = None
) -> ConversationReply:
    paginated = _paginate(question, page)
    label = question.label + paginated.indicator
    text = f"{error}\n\n{label}" if error else label
    return ConversationReply(
        conversation_id=conversation_id,
        substate=ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION,
        text=text,
        choices=paginated.choices,
        input_type=question.input_type,
        validation_rule=question.validation_rule,
    )


async def start_conversation(
    session: AsyncSession,
    *,
    user_id: UUID,
    task_id: UUID,
    task_form_id: UUID,
    form: FormDetail,
    parent_kind: str | None = None,
    parent_choices: list[parent_picker.ParentOption] | None = None,
) -> ConversationReply:
    # parent_kind set means this handler is one of the 5 that need a parent
    # row picked first (router.py already resolved parent_choices and
    # confirmed it's non-empty before calling this -- see
    # src/line/parent_picker.py). The conversation starts with NO open
    # question (current_question_id has a real FK to form.question -- there
    # is no such row for this synthetic step, so it can only ever be NULL or
    # a real question here, never a made-up placeholder). Which picker is
    # pending instead lives in parent_answer as {"pending_kind": ...};
    # handle_answer checks that before its normal current_question_id
    # handling and routes to _handle_parent_answer.
    if parent_kind is not None:
        assert parent_choices, "router.py must not call start_conversation with empty choices"
        conversation = Conversation(
            user_id=user_id,
            task_id=task_id,
            task_form_id=task_form_id,
            status=ConversationStatus.ACTIVE,
            current_question_id=None,
            parent_answer={"pending_kind": parent_kind},
        )
        session.add(conversation)
        await session.commit()
        await session.refresh(conversation)
        return ConversationReply(
            conversation_id=conversation.conversation_id,
            substate=ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION,
            text=parent_picker.PROMPT[parent_kind],
            choices=[_PAUSE_CHOICE, *(Choice(id=o.id, label=o.label) for o in parent_choices)],
        )

    questions = questions_from_form(form)
    first_question = _next_unanswered_required(questions, answered=set())

    conversation = Conversation(
        user_id=user_id,
        task_id=task_id,
        task_form_id=task_form_id,
        status=ConversationStatus.ACTIVE,
        current_question_id=first_question.question_id if first_question else None,
    )
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)

    if first_question is None:
        # No mandatory questions on this form -- nothing to ask, straight to
        # confirmation. A real shape in this sprint's mock data (every
        # processing_record question is is_mandatory=false -- see
        # database/seed/mock_forms.sql's note), not a bug in this engine.
        return ConversationReply(
            conversation_id=conversation.conversation_id,
            substate=ActiveSubstate.AWAITING_CONFIRMATION,
            text="ไม่มีคำถามที่จำเป็นต้องตอบ ยืนยันการส่งข้อมูลหรือไม่?",
        )

    return await _ask_question(
        session, conversation=conversation, question=first_question, form=form
    )


async def start_conversation_with_autofill(
    session: AsyncSession,
    *,
    user_id: UUID,
    task_id: UUID,
    task_form_id: UUID,
    form: FormDetail,
    sanitized_answer: dict[str, Any],
) -> ConversationReply:
    """US2-4 ("offer reusing my last submission's answers") -- same shape as
    start_conversation's no-parent path, except the new conversation's
    answers are seeded from `sanitized_answer` (already run through
    reuse.sanitize_for_autofill by the caller) instead of starting blank.

    Never called for one of the 5 parent-picker handlers: router.py only
    offers autofill when parent_kind is None (a submission's parent row --
    which farm/harvest/batch -- is exactly the kind of thing that must be
    picked fresh each time, never reused, so those handlers keep going
    through plain start_conversation regardless of reuse history).
    """
    conversation = Conversation(
        user_id=user_id,
        task_id=task_id,
        task_form_id=task_form_id,
        status=ConversationStatus.ACTIVE,
        current_question_id=None,
    )
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)

    questions = questions_from_form(form)
    answer_rows = reuse.build_answer_rows(conversation.conversation_id, sanitized_answer, questions)
    for row in answer_rows:
        session.add(row)

    answered = {row.question_id for row in answer_rows}
    next_question = _next_unanswered_required(questions, answered)
    conversation.current_question_id = next_question.question_id if next_question else None
    await session.commit()

    if next_question is None:
        # Every question was covered by the reused answer -- straight to
        # confirmation, same as start_conversation's all-optional-form case.
        # No separate recap prefix: _format_confirmation_summary already
        # lists every answered field (see resume_conversation's identical
        # reasoning for its own current_question_id-is-None branch).
        return ConversationReply(
            conversation_id=conversation.conversation_id,
            substate=ActiveSubstate.AWAITING_CONFIRMATION,
            text=_format_confirmation_summary(questions, answer_rows),
        )

    # Some questions were reused, but at least one still needs asking (a new
    # field on the form, or one reuse.sanitize_for_autofill dropped as
    # stale) -- recap what's already filled in before asking it, same
    # "don't leave a farmer guessing what's already answered" reasoning as
    # resume_conversation's recap.
    recap = _format_resume_recap(questions, answer_rows)
    return ConversationReply(
        conversation_id=conversation.conversation_id,
        substate=ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION,
        text=recap + next_question.label,
        choices=next_question.choices,
        input_type=next_question.input_type,
        validation_rule=next_question.validation_rule,
    )


async def _handle_parent_answer(
    session: AsyncSession,
    *,
    conversation: Conversation,
    raw_text: str,
    parent_kind: str,
    form: FormDetail,
) -> ConversationReply:
    """Resolves an answer to the synthetic parent-picker step (see
    conversation.parent_answer's "pending_kind" shape in start_conversation).
    Recomputes choices fresh rather than trusting whatever was offered when
    the question was asked -- cheap (an indexed, capped-at-13 query) and
    avoids acting on a stale list if the farmer logged something new in
    between.

    On a match: stores the pick on conversation.parent_answer (NOT as a
    ConversationAnswer row -- there's no real form.question backing this
    step, and that table's question_id has a real FK, see
    src/conversation/models.py) and advances to the form's actual first
    question, exactly like start_conversation's non-parent path.
    """
    choices = await parent_picker.choices_for(session, parent_kind, conversation.user_id)
    matched = next((o for o in choices if o.label == raw_text), None)
    if matched is None:
        # Doesn't match any listed choice -- re-ask with a fresh list,
        # same "don't store what can't resolve to a real value" rule
        # handle_answer's own OPTION-question path already follows.
        return ConversationReply(
            conversation_id=conversation.conversation_id,
            substate=ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION,
            text=parent_picker.PROMPT[parent_kind],
            choices=[_PAUSE_CHOICE, *(Choice(id=o.id, label=o.label) for o in choices)],
        )

    conversation.parent_answer = {
        "field_name": parent_picker.FIELD_NAME[parent_kind],
        "value": matched.id,
    }

    questions = questions_from_form(form)
    first_question = _next_unanswered_required(questions, answered=set())
    _advance_to(conversation, first_question.question_id if first_question else None)
    await session.commit()

    if first_question is None:
        return ConversationReply(
            conversation_id=conversation.conversation_id,
            substate=ActiveSubstate.AWAITING_CONFIRMATION,
            text="ไม่มีคำถามที่จำเป็นต้องตอบ ยืนยันการส่งข้อมูลหรือไม่?",
        )
    return await _ask_question(
        session, conversation=conversation, question=first_question, form=form
    )


async def _store_answer_and_advance(
    session: AsyncSession,
    *,
    conversation: Conversation,
    questions: list[Question],
    form: FormDetail,
    answer: dict[str, Any],
) -> ConversationReply:
    """Stores `answer` against the conversation's currently-open question and
    moves on -- to the next question, or to the confirmation summary once
    everything required is in.

    Extracted from handle_answer so the multi-choice picker's "เสร็จ" and skip
    buttons can end a question exactly the way a typed or tapped answer does.
    Those arrive as postbacks, not text, so without this they would each need
    their own copy of the upsert, the state-machine call, the
    confirmation-summary branch and the advance -- four things that have to
    stay identical for the two paths to be indistinguishable downstream, and
    that nobody would remember to keep in sync.
    """
    conversation_id = conversation.conversation_id

    # Upsert, not a blind insert: US2-6's "แก้ไข" (edit-at-confirmation,
    # begin_edit below) can re-open a question that's already been answered
    # once, or skipped -- update that row in place rather than adding a
    # second one for the same question_id, which _format_answered_lines and
    # confirm_conversation's own dict-comprehension would otherwise both
    # silently show/collapse in an order that isn't actually guaranteed (no
    # ORDER BY on _answered_rows, and no unique constraint on
    # (conversation_id, question_id) at the DB level). The normal
    # forward-only flow never finds an existing row here -- current_question_id
    # only ever advances to a not-yet-answered question -- so this is a
    # no-op behavior change for every path except editing.
    answer_rows = await _answered_rows(session, conversation_id)
    existing_row = next(
        (row for row in answer_rows if row.question_id == conversation.current_question_id),
        None,
    )
    if existing_row is not None:
        # Reassign, not mutate -- this column isn't wrapped in
        # sqlalchemy.ext.mutable.MutableDict, so an in-place
        # `existing_row.answer["text"] = ...` wouldn't be seen by the unit
        # of work at commit time.
        existing_row.answer = answer
    else:
        existing_row = ConversationAnswer(
            conversation_id=conversation_id,
            question_id=conversation.current_question_id,
            answer=answer,
            source=AnswerSource.GUIDED_FLOW,
        )
        session.add(existing_row)
        answer_rows.append(existing_row)
    await session.flush()

    answered = _answered_question_ids(answer_rows)
    transition = on_guided_answer(all_slots_filled=_all_required_answered(questions, answered))

    if transition.next_state == ActiveSubstate.AWAITING_CONFIRMATION:
        _advance_to(conversation, None)
        await session.commit()
        return ConversationReply(
            conversation_id=conversation_id,
            substate=ActiveSubstate.AWAITING_CONFIRMATION,
            text=_format_confirmation_summary(questions, answer_rows),
        )

    next_question = _next_unanswered_required(questions, answered)
    if next_question is None:
        # on_guided_answer's contract (constants.py) says this can't happen --
        # "slots remain" and "no unanswered required question found" are
        # contradictory. Fail loudly rather than silently going quiet on the
        # farmer.
        raise RuntimeError(
            f"on_guided_answer reported slots remaining for conversation_id={conversation_id} "
            "but no unanswered mandatory question was found"
        )

    _advance_to(conversation, next_question.question_id)
    await session.commit()
    return await _ask_question(
        session, conversation=conversation, question=next_question, form=form
    )


async def handle_answer(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    raw_text: str,
    form: FormDetail,
) -> ConversationReply | None:
    # Locks the conversation row for the rest of this function -- serializes
    # concurrent webhook deliveries for the same conversation (LINE's own
    # retry-on-slow-response, or a farmer double-tapping/double-texting can
    # otherwise both read the same current_question_id and both write an
    # answer for it before either commits; live-caught 2026-08-09 during the
    # chatbot pathway audit). NOWAIT means a message that arrives while
    # another is still being processed for this conversation is dropped
    # immediately rather than queued -- first message wins, full stop.
    # Reconciling two rapid, genuinely different answers (e.g. a farmer
    # correcting a typo a second later) is deferred to the future LLM-driven
    # flow, which can actually decide what the farmer meant; this guided
    # flow doesn't try.
    try:
        conversation = (
            await session.execute(
                select(Conversation)
                .where(Conversation.conversation_id == conversation_id)
                .with_for_update(nowait=True)
            )
        ).scalar_one_or_none()
    except DBAPIError as exc:
        # 55P03 = lock_not_available (Postgres SQLSTATE) -- checked on the
        # code rather than the wrapped exception type, since SQLAlchemy's
        # asyncpg dialect re-wraps the driver error (AsyncAdapt_asyncpg_dbapi
        # .Error, not asyncpg's own LockNotAvailableError) but still proxies
        # .sqlstate through from the original.
        if getattr(exc.orig, "sqlstate", None) != "55P03":
            raise
        logger.info(
            "conversation_id=%s already has an answer in flight -- dropping this message",
            conversation_id,
        )
        return None
    if conversation is None:
        raise ConversationNotFound()

    # Checked before everything else below (parent-picker step, no open
    # question, or a real question) -- pause (US2-3) works identically
    # regardless of where in the flow the farmer currently is. A raw-text
    # match rather than a per-question choice list, since the picker step
    # and "no open question left" cases don't have a current_question to
    # attach the button to at all. Doesn't touch current_question_id or
    # write a ConversationAnswer -- pausing isn't answering.
    if raw_text == _PAUSE_CHOICE.label:
        conversation.status = ConversationStatus.PAUSED
        await session.commit()
        return ConversationReply(
            conversation_id=conversation_id,
            substate=ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION,
            text='พักงานนี้ไว้ให้แล้ว คำตอบที่ตอบไปแล้วถูกบันทึกครบถ้วน พิมพ์ "เริ่ม" เพื่อดูรายการงานเมื่อพร้อมทำต่อ',
        )

    # Checked before the current_question_id==None check below: the picker
    # step deliberately leaves current_question_id NULL (see
    # start_conversation), so without this, a pending pick would be
    # misread as "no open question" instead of routed to the picker.
    pending_kind = (conversation.parent_answer or {}).get("pending_kind")
    if pending_kind is not None:
        return await _handle_parent_answer(
            session,
            conversation=conversation,
            raw_text=raw_text,
            parent_kind=pending_kind,
            form=form,
        )

    if conversation.current_question_id is None:
        raise ConversationNotFound("Conversation has no open question to answer")

    questions = questions_from_form(form)
    question_by_id = {q.question_id: q for q in questions}
    current_question = question_by_id.get(conversation.current_question_id)

    # The multi-choice picker owns this question while it is open: its answer
    # is built by tapping, so typed text is interpreted against the picker's
    # own options here rather than falling through to the OPTION matching
    # below -- which, for the plot question, would match against Kotlin's
    # global ref.plot_constant list, i.e. plots that are not this farmer's.
    if current_question is not None:
        picker_options = await multi_choice_options_for(
            session, conversation=conversation, question=current_question, form=form
        )
        if picker_options is not None:
            return await _handle_typed_selection_text(
                session,
                conversation=conversation,
                question=current_question,
                options=picker_options,
                form=form,
                raw_text=raw_text,
            )

    resolved_value: str | None = None
    is_skip = False
    if current_question is not None and current_question.choices:
        # Matched against whichever page is actually showing right now --
        # NOT current_question.choices directly, which is always the page-0
        # rendering. A stale/page-0 match here would accept a label the
        # farmer can't actually see as a button on their current page (or
        # reject one they're genuinely looking at).
        page_choices = _paginate(current_question, conversation.current_page).choices
        # Exact match first (unchanged from before text_parsing existed),
        # then a conservative fuzzy fallback for a close-but-not-exact
        # phrasing -- see match_choice's own docstring.
        matched = text_parsing.match_choice(raw_text, page_choices or [])
        if matched is not None and matched.id in (_NEXT_PAGE_CHOICE_ID, _PREV_PAGE_CHOICE_ID):
            # Paging isn't answering -- current_question_id, every
            # already-given answer, and the rest of this question stay
            # exactly as they are. Only current_page moves.
            conversation.current_page += 1 if matched.id == _NEXT_PAGE_CHOICE_ID else -1
            await session.commit()
            return _reply_for_question(
                conversation_id, current_question, page=conversation.current_page
            )
        if matched is not None and matched.id == _SKIP_CHOICE_ID:
            is_skip = True
        elif current_question.has_constrained_choices:
            if matched is None:
                # Doesn't match any listed choice -- re-ask rather than store
                # text that can't resolve to a real domain value later. Keeps
                # the same question AND the same page open, same choices
                # offered again.
                return _reply_for_question(
                    conversation_id,
                    current_question,
                    page=conversation.current_page,
                    error="กรุณาเลือกคำตอบจากตัวเลือกที่กำหนดเท่านั้น",
                )
            resolved_value = matched.id
        # else: non-mandatory free-text question offering only a skip button
        # -- typed text that isn't the skip label is a real answer, not a
        # mismatch, so it falls through to the normal free-text path below.

    # Format validation (the Validate Answer step) only applies to genuine
    # free text -- a skip is an intentional absence (nothing to check), and
    # a resolved OPTION/BOOLEAN choice is already constrained to a
    # known-good value by construction (matched against current_question's
    # own choices above).
    # What actually gets validated and stored -- raw_text unless a fixed
    # parser below normalizes it (a spelled-out Thai number/date into the
    # digit/ISO form validate_answer's own INT/FLOAT/DATE/DATETIME
    # validators expect). Declared before the block below so it's always
    # defined, including the is_skip/resolved_value/no-open-question cases
    # that skip that block entirely.
    text_for_validation = raw_text
    if not is_skip and resolved_value is None and current_question is not None:
        # A mandatory free-text question offers no skip button (see
        # _choices_for), so blank/whitespace-only text is never an
        # intentional skip -- it's an empty non-answer. Checked ahead of
        # validate_answer since a field with no validation_rule row at all
        # (or a VARCHAR rule with no min_length) would otherwise let it
        # through unchecked, defeating the point of gating the DB write on
        # validation (US2-2).
        if current_question.is_mandatory and not raw_text.strip():
            return _reply_for_question(
                conversation_id,
                current_question,
                page=conversation.current_page,
                error="กรุณาตอบคำถามนี้ ไม่สามารถเว้นว่างได้",
            )

        # (New) Fixed-parse step (docs/plans/text-parsing-pipeline.md):
        # for a field whose rule is a type text_parsing knows how to
        # pre-parse, try that BEFORE validate_answer -- "เก้าร้อย"/"วันนี้"
        # normalized into "900"/an ISO date, so validate_answer only ever
        # sees the same digit/ISO shapes it always has. An already-valid
        # answer (the common case) round-trips through parse_number/
        # parse_date unchanged -- see those functions' own "already valid"
        # fast path -- so this never changes behavior for it. On a MISS
        # (neither library could make sense of it), text_for_validation
        # simply stays raw_text -- validate_answer rejects that on its own
        # terms, with the field's own configured error_message, same as it
        # always has for any other invalid answer. Deliberately NOT a
        # separate "couldn't understand" message here: that would show
        # different wording depending on WHY the field rejected the answer,
        # and (live-caught by this module's own test suite) would override
        # a field's own more specific guidance with a generic one.
        rule = current_question.validation_rule
        rule_type = (rule or {}).get("type")
        if rule_type in text_parsing.FIXED_PARSE_TYPES:
            parsed = text_parsing.try_fixed_parse(rule_type, raw_text)
            parse_path = "fixed"
            if parsed is None:
                # Fixed parser couldn't make sense of it -- last resort is
                # the LLM (llm_parsing.py). Its result still goes through
                # validate_answer below like any other answer.
                parsed = await llm_parsing.try_llm_parse(rule_type, raw_text)
                parse_path = "llm"
            if parsed is not None:
                text_for_validation = parsed
            else:
                parse_path = "none"
            logger.info("text parse rule_type=%s path=%s", rule_type, parse_path)

        error = validate_answer(rule, text_for_validation)
        if error is not None:
            return _reply_for_question(
                conversation_id, current_question, page=conversation.current_page, error=error
            )

    if is_skip:
        answer: dict[str, Any] = {"skipped": True}
    else:
        answer = {"text": text_for_validation}
    if resolved_value is not None:
        answer["value"] = resolved_value

    return await _store_answer_and_advance(
        session, conversation=conversation, questions=questions, form=form, answer=answer
    )


def _nothing_submitted(conversation_id: UUID, text: str) -> ConversationReply:
    """A plain-text reply for a confirm that wasn't one (see
    ConversationReply.nothing_submitted). GUIDED_ASKING_FIXED_QUESTION with no
    choices, so the router's _reply sends it as text with no confirm button
    pointing at a conversation that's already finished.
    """
    return ConversationReply(
        conversation_id=conversation_id,
        substate=ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION,
        text=text,
        nothing_submitted=True,
    )


def _already_submitted(answer_rows: list[ConversationAnswer]) -> list[str]:
    """Which rows of a several-answer submission have already reached Go --
    empty unless a confirm stopped partway.
    """
    row = _multi_choice_row(answer_rows)
    if row is None:
        return []
    return [str(value) for value in row.answer.get("submitted", [])]


def _multi_choice_row(answer_rows: list[ConversationAnswer]) -> ConversationAnswer | None:
    """The answer that holds several choices, if this conversation has one --
    there is at most one (see multi_choice_options_for).

    A single-choice selection is stored as an ordinary {"text", "value"}
    answer (see _multi_choice_answer), so it never reaches this and never
    changes how the submission is built.
    """
    return next((row for row in answer_rows if row.answer.get("multi")), None)


async def _submit_per_choice(
    session: AsyncSession,
    *,
    conversation: Conversation,
    row: ConversationAnswer,
    field_name: str,
    base_payload: dict[str, Any],
) -> ConversationReply | None:
    """Writes one row per ticked answer: the same answers N times, differing
    only in `field_name`. Returns None when every row made it, or the reply
    to send when it stopped partway.

    The whole design rests on `submitted`: each choice is appended to it and
    COMMITTED the moment Go accepts that row, so a retry re-sends only what is
    genuinely missing. Without it, a farmer whose 3rd row failed would, on
    tapping ยืนยัน again, write rows 1 and 2 a second time -- and Go cannot
    protect them, because SubmitTaskForUser deliberately allows repeat
    submissions (that is what makes multi-submit forms work at all) and
    dissectAnswer has no idempotency guard. Duplicated farm records are worse
    than a failed save: nobody notices them.

    Sequential, not gathered: per-row commit ordering is the point, and a
    handful of rows inside one webhook request is well within the reply
    window. HandlerNotSupported is deliberately re-raised -- it is a
    permanent "this form isn't built yet", it fails on the first row before
    anything is written, and confirm_conversation's own branch already says
    that honestly.
    """
    values = [str(value) for value in row.answer.get("values", [])]
    labels = [str(label) for label in row.answer.get("labels", [])]
    submitted = [str(value) for value in row.answer.get("submitted", [])]

    for choice_id in values:
        if choice_id in submitted:
            # Already saved on an earlier attempt -- skipping it is what
            # keeps a retry from duplicating rows.
            continue
        try:
            await submit_task(
                TaskSubmission(
                    user_id=str(conversation.user_id),
                    task_id=str(conversation.task_id),
                    answer={**base_payload, field_name: choice_id},
                )
            )
        except HandlerNotSupported:
            # Permanent and identical for every row -- let
            # confirm_conversation's own branch report it. Nothing has been
            # written, because this fails on the first row.
            raise
        except Exception:
            logger.exception(
                "submit_task failed for conversation_id=%s on row %d of %d -- "
                "%d row(s) already saved, conversation left awaiting confirmation so "
                "tapping confirm again retries only the rest",
                conversation.conversation_id,
                values.index(choice_id) + 1,
                len(values),
                len(submitted),
            )
            if not submitted:
                # Nothing landed at all -- this is an ordinary failed submit
                # from the farmer's point of view, so give them the ordinary
                # message rather than "saved 0 of 3".
                return ConversationReply(
                    conversation_id=conversation.conversation_id,
                    substate=ActiveSubstate.AWAITING_CONFIRMATION,
                    text="เกิดข้อผิดพลาด ไม่สามารถบันทึกข้อมูลได้ กรุณาลองใหม่อีกครั้ง",
                    submission_failed=True,
                )
            saved_labels = [labels[values.index(saved)] for saved in submitted if saved in values]
            return ConversationReply(
                conversation_id=conversation.conversation_id,
                substate=ActiveSubstate.AWAITING_CONFIRMATION,
                text=(
                    f"บันทึกแล้ว {len(submitted)} จาก {len(values)} รายการ "
                    f"({', '.join(saved_labels)}) "
                    "— กดยืนยันอีกครั้งเพื่อบันทึกรายการที่เหลือ"
                ),
                submission_failed=True,
            )

        submitted = [*submitted, choice_id]
        # Reassign + commit per row, not once at the end: a crash, a Vercel
        # timeout, or the farmer's next tap must never be able to lose the
        # record of what already reached Go.
        row.answer = {**row.answer, "submitted": submitted}
        await session.commit()

    return None


_ALREADY_SAVED = "บันทึกข้อมูลชุดนี้ไปแล้วครับ"
_ALREADY_CANCELLED = "รายการนี้ถูกยกเลิกไปแล้วครับ"
_CONFIRM_NOT_READY = "ยังตอบไม่ครบครับ กรุณาตอบข้อนี้ให้เสร็จก่อนกดยืนยัน"


async def confirm_conversation(
    session: AsyncSession, *, conversation_id: UUID, form: FormDetail
) -> ConversationReply:
    # Locked, and WAITING for the lock (not NOWAIT): a double tap on ยืนยัน,
    # or LINE redelivering the postback, arrives as two independent webhook
    # requests. Unlocked, both read `submitted` before either wrote it, both
    # saw nothing sent, and both sent every row -- 3 plots became 6 rows
    # (review on #76). With the lock the second request waits for the first
    # to finish, then sees the conversation COMPLETED (or `submitted`
    # already filled in) and sends nothing twice. Waiting rather than
    # dropping it, so the second tap still gets an honest answer.
    #
    # populate_existing is load-bearing: the router has usually already
    # loaded this row in the same session (session.get, to find the form)
    # BEFORE the lock was taken. Without it SQLAlchemy hands back that
    # already-loaded object with its pre-lock attributes, so the waiting
    # request would still see status ACTIVE and submit everything again.
    conversation = (
        await session.execute(
            select(Conversation)
            .where(Conversation.conversation_id == conversation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if conversation is None:
        raise ConversationNotFound()

    if conversation.status == ConversationStatus.COMPLETED:
        return _nothing_submitted(conversation_id, _ALREADY_SAVED)
    if conversation.status == ConversationStatus.CANCELLED:
        return _nothing_submitted(conversation_id, _ALREADY_CANCELLED)

    # Only the confirmation step can be confirmed. A ยืนยัน button can never
    # be retracted from the chat, so an old one stays tappable after the
    # farmer has gone back into a question (แก้ไข, or a selection still being
    # built). Submitting then would send whatever half-finished state that
    # question is in -- e.g. a plot selection with nothing ticked yet goes
    # out as plot_id missing, i.e. a whole-farm record nobody asked for
    # (review on #76). Show them the step they're actually on instead.
    picker_pending = (conversation.parent_answer or {}).get("pending_kind") is not None
    if picker_pending or conversation.current_question_id is not None:
        current_step = await resume_conversation(session, conversation=conversation, form=form)
        return replace(
            current_step,
            text=f"{_CONFIRM_NOT_READY}\n\n{current_step.text}",
            nothing_submitted=True,
        )

    field_name_by_question_id = {q.question_id: q.field_name for q in questions_from_form(form)}
    # A selection still being built ({"selecting": true}) is never part of a
    # submission. The state check above already rules this out; this is the
    # backstop, so no future path can ship an unfinished answer to Go.
    answer_rows = [
        row
        for row in await _answered_rows(session, conversation_id, refresh=True)
        if not row.answer.get("selecting")
    ]
    multi_row = _multi_choice_row(answer_rows)
    # Skipped fields are saved as nothing -- omitted from the payload
    # entirely rather than sending an empty/null value for that column. The
    # multi-choice row is omitted too and added back one choice at a time
    # below; it is the single field that differs between the N rows written.
    answer_payload: dict[str, Any] = {
        field_name_by_question_id.get(row.question_id, str(row.question_id)): (
            row.answer.get("value") or row.answer.get("text")
        )
        for row in answer_rows
        if not row.answer.get("skipped") and row is not multi_row
    }
    if conversation.parent_answer is not None and "field_name" in conversation.parent_answer:
        parent_field_name = conversation.parent_answer["field_name"]
        answer_payload[parent_field_name] = conversation.parent_answer["value"]

    try:
        if multi_row is not None:
            partial = await _submit_per_choice(
                session,
                conversation=conversation,
                row=multi_row,
                field_name=field_name_by_question_id.get(
                    multi_row.question_id, str(multi_row.question_id)
                ),
                base_payload=answer_payload,
            )
            if partial is not None:
                return partial
        else:
            await submit_task(
                TaskSubmission(
                    user_id=str(conversation.user_id),
                    task_id=str(conversation.task_id),
                    answer=answer_payload,
                )
            )
    except HandlerNotSupported:
        # A permanent failure, not a transient one -- Go is correctly saying
        # "not built yet" (see docs/plans/chatbot-child-handler-design.md),
        # not reporting a bug or outage. Retrying this exact submission will
        # never succeed, so the generic "ลองใหม่อีกครั้ง" (try again) message
        # below would be actively misleading here -- tell the farmer the
        # real reason instead. Still leaves the conversation awaiting
        # confirmation (not COMPLETED) so the farmer's own cancel button
        # works normally; this is a case not-yet-implemented, the pre-flight
        # check in src/line/router.py should catch known cases earlier than
        # this for the 5 handlers it knows about, but this is the honest
        # fallback for any handler Go itself reports unsupported.
        logger.info(
            "submit_task failed for conversation_id=%s -- handler not supported yet, "
            "telling the farmer honestly instead of suggesting a retry",
            conversation_id,
        )
        return ConversationReply(
            conversation_id=conversation_id,
            substate=ActiveSubstate.AWAITING_CONFIRMATION,
            text="ขออภัย แบบฟอร์มประเภทนี้ยังไม่รองรับการบันทึกอัตโนมัติ ทีมงานกำลังพัฒนาอยู่ คำตอบของคุณจะไม่ถูกบันทึก",
            submission_failed=True,
        )
    except Exception:
        # Go's dissection logic is real now (not a stub -- see tasks/client.py's
        # docstring), so a failure here means an actual problem worth reading
        # the exception message for (bad service key, no matching
        # chat.conversation, etc.) -- not an expected gap.
        #
        # Deliberately NOT marking the conversation COMPLETED here, and NOT
        # telling the farmer it saved -- it didn't. A prior version of this
        # code swallowed the failure and reported success anyway, which meant
        # total, undetectable data loss (confirmed live during the 2026-08-09
        # pathway audit: Go rejected the write, zero rows landed anywhere, and
        # the farmer was told "saved"). The conversation stays exactly where
        # it was (still awaiting confirmation) so tapping the confirm button
        # again just retries this same submit_task call.
        logger.exception(
            "submit_task failed for conversation_id=%s -- chatbot-side answers are still "
            "saved, but nothing was written to Go/the domain table; conversation left "
            "awaiting confirmation for retry",
            conversation_id,
        )
        return ConversationReply(
            conversation_id=conversation_id,
            substate=ActiveSubstate.AWAITING_CONFIRMATION,
            text="เกิดข้อผิดพลาด ไม่สามารถบันทึกข้อมูลได้ กรุณาลองใหม่อีกครั้ง",
            submission_failed=True,
        )

    conversation.status = ConversationStatus.COMPLETED
    await session.commit()

    # A multi-choice confirm ends the submission outright -- no "➕ เพิ่มอีก
    # รายการ". The farmer has just ticked every answer they had in one go;
    # offering the one-row-at-a-time loop on top of that would put two ways of
    # doing the same thing in front of them, and a second fan-out from the
    # same carried-forward answers is exactly the duplicate this feature was
    # careful not to create. Returned as the ordinary terminal reply, so the
    # caller sends the quick ack and the diary just as for any single-submit
    # form -- the task itself stays listed under "เริ่ม" until close_at, as
    # every multi-submit task does, if they genuinely have more to record.
    if multi_row is not None:
        row_count = len(multi_row.answer.get("values", []))
        return ConversationReply(
            conversation_id=conversation_id,
            substate=ActiveSubstate.AWAITING_CONFIRMATION,
            text=f"บันทึกข้อมูลเรียบร้อยแล้ว {row_count} รายการ ขอบคุณครับ",
        )

    # This conversation is done either way -- the row is saved. A
    # multiple-submit form just isn't finished with the TASK: the farmer is
    # expected to file several rows against it (grade A, then B, then C), so
    # offer another instead of closing the conversation out. The caller
    # attaches the buttons; router.py's "add_another" postback starts the
    # next one with the parent selection carried forward. Reached only when
    # the picker was NOT used for several answers -- including a picker
    # where the farmer ticked just one, which is stored as an ordinary answer.
    if form.is_multiple_submit:
        return ConversationReply(
            conversation_id=conversation_id,
            substate=ActiveSubstate.AWAITING_CONFIRMATION,
            text="บันทึกข้อมูลเรียบร้อยแล้ว ต้องการเพิ่มอีกรายการสำหรับงานนี้หรือไม่?",
            offer_another=True,
        )

    return ConversationReply(
        conversation_id=conversation_id,
        substate=ActiveSubstate.AWAITING_CONFIRMATION,
        text="บันทึกข้อมูลเรียบร้อยแล้ว ขอบคุณครับ",
    )


def _carried_answer(answer: dict[str, Any]) -> dict[str, Any]:
    """The copy of an answer that carries forward into the next submission.

    Everything about WHAT the farmer answered is copied, including a
    multi-choice selection. `submitted` is deliberately dropped: it records
    which rows of the PREVIOUS submission already reached Go, and carrying
    it would make the new conversation skip exactly those -- silently
    writing fewer rows than the farmer picked.
    """
    return {key: value for key, value in answer.items() if key != "submitted"}


async def start_next_submission(
    session: AsyncSession, *, conversation_id: UUID, form: FormDetail
) -> ConversationReply:
    """The "➕ เพิ่มอีกรายการ" half of multi-submit: opens a FRESH conversation
    on the same task and form as the one just confirmed.

    The point of doing this here rather than routing back through
    start_conversation is parent_answer. All five handlers that actually
    want repeat submission are the child-row handlers whose parent FK is NOT
    NULL, so every row needs its harvest_id/farm_activity_id/batch_id just
    as much as the first did -- but re-running the parent picker would ask a
    farmer to re-pick the same harvest before every single grade, which is
    the difference between a feature and an annoyance. The already-resolved
    parent_answer is copied forward instead, so the next row starts at the
    form's first real question.

    Deliberately a new Conversation row rather than reopening the completed
    one: the finished submission stays exactly as it was submitted, and each
    row keeps its own answer history.
    """
    previous = await session.get(Conversation, conversation_id)
    if previous is None:
        raise ConversationNotFound()

    # Only a RESOLVED parent carries forward. A conversation still sitting
    # on {"pending_kind": ...} never picked one, and copying that would
    # start the next submission stuck at the picker step.
    parent_answer = previous.parent_answer
    if parent_answer is not None and "field_name" not in parent_answer:
        parent_answer = None

    questions = questions_from_form(form)

    # Carry-forward: questions the form author flagged keep the answer from
    # the submission just confirmed, so a farmer logging three activities on
    # one plot picks the plot once. Only answers that actually exist are
    # copied -- a flagged question the farmer skipped last time is asked.
    flagged = {q.question_id for q in questions if q.carry_forward}
    carried = (
        [
            row
            for row in await _answered_rows(session, conversation_id)
            if row.question_id in flagged
        ]
        if flagged
        else []
    )
    answered = {row.question_id for row in carried}
    first_question = _next_unanswered_required(questions, answered=answered)

    conversation = Conversation(
        user_id=previous.user_id,
        task_id=previous.task_id,
        task_form_id=previous.task_form_id,
        status=ConversationStatus.ACTIVE,
        current_question_id=first_question.question_id if first_question else None,
        parent_answer=parent_answer,
    )
    session.add(conversation)
    await session.flush()
    for row in carried:
        session.add(
            ConversationAnswer(
                conversation_id=conversation.conversation_id,
                question_id=row.question_id,
                answer=_carried_answer(row.answer),
                source=row.source,
            )
        )
    await session.commit()
    await session.refresh(conversation)

    if first_question is None:
        return ConversationReply(
            conversation_id=conversation.conversation_id,
            substate=ActiveSubstate.AWAITING_CONFIRMATION,
            text="ไม่มีคำถามที่จำเป็นต้องตอบ ยืนยันการส่งข้อมูลหรือไม่?",
        )
    return await _ask_question(
        session, conversation=conversation, question=first_question, form=form
    )


async def cancel_conversation(session: AsyncSession, *, conversation_id: UUID) -> ConversationReply:
    """The escape hatch confirm/retry didn't have -- added after a farmer got
    stuck retrying a submission that could never succeed (an unsupported
    handler, CB-1's honest-failure message correctly refusing to lie about
    it, but with no way out other than retrying forever). Nothing to submit
    here -- just marks the conversation CANCELLED so `เริ่ม` can start a
    fresh one for the same task (the active-conversation lookup only
    matches status == ACTIVE).
    """
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise ConversationNotFound()

    conversation.status = ConversationStatus.CANCELLED
    _advance_to(conversation, None)
    await session.commit()

    return ConversationReply(
        conversation_id=conversation_id,
        substate=ActiveSubstate.AWAITING_CONFIRMATION,
        text="ยกเลิกแล้ว ไม่ได้บันทึกข้อมูล",
    )


async def pause_active_conversation(session: AsyncSession, *, user_id: UUID) -> None:
    """Called whenever the farmer types เริ่ม (US2-3) -- since เริ่ม now always
    shows the task list, whatever's currently ACTIVE for this farmer needs
    to be paused first so switching to look at the list never silently
    loses it. A no-op if nothing is active. Distinct from
    src/conversation/jobs.py's daily sweep: that one is the backstop for
    "went quiet and never came back"; this one is the immediate, same-turn
    "farmer is deliberately switching tasks right now" case.
    """
    conversation = (
        (
            await session.execute(
                select(Conversation).where(
                    Conversation.user_id == user_id,
                    Conversation.status == ConversationStatus.ACTIVE,
                )
            )
        )
        .scalars()
        .first()
    )
    if conversation is None:
        return
    conversation.status = ConversationStatus.PAUSED
    await session.commit()


async def find_resumable_conversation(
    session: AsyncSession, *, user_id: UUID, task_id: UUID
) -> Conversation | None:
    """Whether this (farmer, task) pair already has a conversation that
    isn't done -- ACTIVE (mid-turn, rare to hit here since เริ่ม just paused
    it) or PAUSED. Used by the "start" postback to decide resume vs a fresh
    start_conversation; COMPLETED/CANCELLED rows are correctly excluded, so
    a farmer can always begin again after either of those.
    """
    return (
        (
            await session.execute(
                select(Conversation).where(
                    Conversation.user_id == user_id,
                    Conversation.task_id == task_id,
                    Conversation.status.in_([ConversationStatus.ACTIVE, ConversationStatus.PAUSED]),
                )
            )
        )
        .scalars()
        .first()
    )


async def resume_conversation(
    session: AsyncSession, *, conversation: Conversation, form: FormDetail
) -> ConversationReply:
    """Picks a paused (or still-active) conversation back up (US2-3's
    resume-conversation logic) -- never silently: the reply always leads
    with a recap of what's already been answered, since LINE's own Quick
    Reply buttons from the original question are long gone from the chat UI
    by the time a farmer comes back, and they may not remember where they
    left off.

    Reuses whichever "what happens next" the conversation was already
    sitting at -- the parent-picker step, a real question, or the
    confirmation summary -- rather than reimplementing that branching here.
    """
    conversation.status = ConversationStatus.ACTIVE
    await session.commit()

    questions = questions_from_form(form)
    answer_rows = await _answered_rows(session, conversation.conversation_id)
    recap = _format_resume_recap(questions, answer_rows)

    pending_kind = (conversation.parent_answer or {}).get("pending_kind")
    if pending_kind is not None:
        choices = await parent_picker.choices_for(session, pending_kind, conversation.user_id)
        return ConversationReply(
            conversation_id=conversation.conversation_id,
            substate=ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION,
            text=recap + parent_picker.PROMPT[pending_kind],
            choices=[_PAUSE_CHOICE, *(Choice(id=o.id, label=o.label) for o in choices)],
        )

    if conversation.current_question_id is None:
        # Paused right at the confirmation step (or an all-optional form
        # with nothing left to ask) -- the summary already IS "recap +
        # what's needed now" in one, so no separate recap prefix here.
        return ConversationReply(
            conversation_id=conversation.conversation_id,
            substate=ActiveSubstate.AWAITING_CONFIRMATION,
            text=_format_confirmation_summary(questions, answer_rows),
        )

    question_by_id = {q.question_id: q for q in questions}
    current_question = question_by_id.get(conversation.current_question_id)
    if current_question is None:
        # The form changed out from under a long-paused conversation (a
        # question was removed/edited) -- fail loudly rather than silently
        # asking about a question that no longer exists.
        raise ConversationNotFound(
            f"Resumed conversation_id={conversation.conversation_id}'s current question "
            "is no longer part of the form"
        )
    picker_options = await multi_choice_options_for(
        session, conversation=conversation, question=current_question, form=form
    )
    if picker_options is not None:
        # Resuming mid-selection: the bubble comes back with what the farmer
        # had already ticked still ticked, because the selection lives in the
        # answer row rather than in the (unretractable, uneditable) message
        # they tapped before pausing.
        values, _ = await _current_selection(session, conversation)
        return _multi_choice_reply(
            conversation.conversation_id,
            current_question,
            picker_options,
            selected=frozenset(values),
            prefix=recap,
        )

    # Page-aware: a farmer paused mid-pagination resumes on the exact page
    # they left, not silently back at page 1 (conversation.current_page
    # persists across pause/resume same as everything else).
    paginated = _paginate(current_question, conversation.current_page)
    return ConversationReply(
        conversation_id=conversation.conversation_id,
        substate=ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION,
        text=recap + current_question.label + paginated.indicator,
        choices=paginated.choices,
        input_type=current_question.input_type,
        validation_rule=current_question.validation_rule,
    )


async def editable_questions(
    session: AsyncSession, *, conversation_id: UUID, form: FormDetail
) -> list[Question]:
    """Every question a farmer can currently pick to revisit via "แก้ไข"
    (US2-6, edit-at-confirmation) -- both real answers AND intentionally-
    skipped ones (picking a skipped one just means "answer it now"; skip
    only ever meant "not right now", not "never"). Sorted the same way
    _format_answered_lines already orders things, so the picker's order
    matches what a farmer just read in the confirmation summary.
    """
    questions = questions_from_form(form)
    question_by_id = {q.question_id: q for q in questions}
    answer_rows = await _answered_rows(session, conversation_id)
    answered_ids = {row.question_id for row in answer_rows if row.question_id in question_by_id}
    ordered = sorted(answered_ids, key=lambda qid: question_by_id[qid].sort_order)
    return [question_by_id[qid] for qid in ordered]


async def edit_blocked_reason(session: AsyncSession, *, conversation_id: UUID) -> str | None:
    """Why แก้ไข isn't allowed right now, or None if it is.

    Blocked exactly when a several-answer confirm stopped partway: some rows
    are already in Go, the rest aren't. Editing then can only go wrong
    (review on #76):
    - a fresh selection is stored as a new answer with an empty
      `submitted`, so the next confirm re-sends the rows that already
      landed -- duplicate farm records, which Go can't catch because
      repeat submissions are allowed by design;
    - and even without that, the chat cannot change rows Go already wrote,
      so letting the farmer "edit" them would promise something untrue.
    The farmer can still confirm (sends only what's missing) or cancel.
    """
    already = _already_submitted(await _answered_rows(session, conversation_id))
    if not already:
        return None
    return (
        f"บันทึกไปแล้ว {len(already)} รายการ จึงแก้ไขคำตอบไม่ได้ครับ "
        "กดยืนยันเพื่อบันทึกรายการที่เหลือ หรือกดยกเลิก "
        "(รายการที่บันทึกแล้วจะยังอยู่ในระบบ)"
    )


async def begin_edit(
    session: AsyncSession, *, conversation_id: UUID, question_id: UUID, form: FormDetail
) -> ConversationReply:
    """US2-6: re-opens an already-answered (or skipped) question for editing
    from the confirmation step. Sets current_question_id back to it and
    sends the exact same question UI a farmer saw the first time around
    (same choices/skip/pause buttons) -- handle_answer needs no changes
    beyond the upsert it already does to land back at AWAITING_CONFIRMATION
    once it's answered again: every other required question is already
    filled, so on_guided_answer's own "all slots filled" check does that
    automatically, with no separate "am I editing" state to track.
    """
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise ConversationNotFound()

    questions = questions_from_form(form)
    # Checked here as well as at the แก้ไข button (edit_blocked_reason): the
    # question picker that leads here can't be retracted, so an old one may
    # still be tapped after part of the submission went through.
    blocked = await edit_blocked_reason(session, conversation_id=conversation_id)
    if blocked is not None:
        answer_rows = await _answered_rows(session, conversation_id)
        return ConversationReply(
            conversation_id=conversation_id,
            substate=ActiveSubstate.AWAITING_CONFIRMATION,
            text=f"{blocked}\n\n{_format_confirmation_summary(questions, answer_rows)}",
        )

    question_by_id = {q.question_id: q for q in questions}
    question = question_by_id.get(question_id)
    if question is None:
        # The form changed out from under this conversation between the
        # picker being sent and the farmer tapping it -- same "fail loudly"
        # precedent as resume_conversation's own current_question lookup.
        raise ConversationNotFound(
            f"conversation_id={conversation_id}: question_id={question_id} is no longer "
            "part of the form"
        )

    conversation.current_question_id = question_id
    await session.commit()
    return await _ask_question(session, conversation=conversation, question=question, form=form)
