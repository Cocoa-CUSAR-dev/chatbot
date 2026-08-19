"""Farm/station-scoped picker for the 5 previously-blocked child handlers --
farm_activity_fertilizer, farm_activity_chemical, harvest_grade_detail,
fermentation_batch, drying_batch. Each needs a parent row ID (a farm
activity, harvest, or batch) that isn't part of the form's own questions;
the farmer picks it from a list scoped to their own farm/station, via the
membership tables confirmed with the original schema designer (เป็นเอก
สิทธิมงคล): agriculture.farmer_farm, processing.hub_collector,
processing.processor_processing_station. See
docs/plans/chatbot-child-handler-design.md for the full design.

harvest_grade_detail is the one case that answer didn't fully resolve --
collection.harvest carries BOTH farm_id and hub_id, and grading may be done
by the farmer or by hub staff. Rather than blocking on that decision, the
harvest picker shows the union of both memberships: offered if the
requester is on its farm OR works at its hub. Showing one extra choice is a
much smaller problem than hiding the right one -- worth narrowing later if
it turns out one side never applies in practice.

Deliberate, permanent exception to "the chatbot never touches form.*
directly" (ADR 0001) -- these queries read agriculture.*/collection.*/
processing.* directly rather than form.*, which is a different domain Go
owns for writes but never exposed a scoped read API for. Same shape of
exception src/line/temp_task_picker.py already takes, but that one is
explicitly temporary scaffolding; this one is the actual recommended
design, not a stand-in for something else shipping later.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_QUICK_REPLY_LIMIT = 13  # LINE's own cap on Quick Reply items per message

_PARENT_KIND_BY_HANDLER = {
    "farm_activity_fertilizer": "farm_activity",
    "farm_activity_chemical": "farm_activity",
    "harvest_grade_detail": "harvest",
    "fermentation_batch": "batch",
    "drying_batch": "batch",
}

# The real destination column on each handler's own table -- once picked,
# this rides through confirm_conversation's answer payload exactly like any
# other field (see docs' "field_name trick").
FIELD_NAME = {
    "farm_activity": "farm_activity_id",
    "harvest": "harvest_id",
    "batch": "batch_id",
}

PROMPT = {
    "farm_activity": "เลือกกิจกรรมในฟาร์มที่เกี่ยวข้อง:",
    "harvest": "เลือกการเก็บเกี่ยวที่เกี่ยวข้อง:",
    "batch": "เลือกแบทช์การแปรรูปที่เกี่ยวข้อง:",
}

EMPTY_PROMPT = {
    "farm_activity": "คุณยังไม่มีกิจกรรมในฟาร์มที่บันทึกไว้เลย กรุณาบันทึกกิจกรรมในฟาร์มก่อน",
    "harvest": "คุณยังไม่มีการเก็บเกี่ยวที่บันทึกไว้เลย กรุณาบันทึกการเก็บเกี่ยวก่อน",
    "batch": "คุณยังไม่มีแบทช์การแปรรูปที่บันทึกไว้เลย กรุณาบันทึกแบทช์ก่อน",
}


@dataclass(frozen=True)
class ParentOption:
    id: str
    label: str


def kind_for_handler(handler: str) -> str | None:
    return _PARENT_KIND_BY_HANDLER.get(handler)


def _label(prefix: str, when: datetime, place: str) -> str:
    return f"{prefix} {when:%d/%m/%Y} — {place}"


async def _farm_activity_choices(session: AsyncSession, user_id: UUID) -> list[ParentOption]:
    rows = await session.execute(
        text(
            """
            SELECT fa.farm_activity_id AS id, fa.created_at, f.farm_name,
                   COALESCE(t.farm_activity_type_name, '') AS type_name
            FROM agriculture.farm_activity fa
            JOIN agriculture.farmer_farm ff ON ff.farm_id = fa.farm_id
            JOIN agriculture.farm f ON f.farm_id = fa.farm_id
            LEFT JOIN ref.farm_activity_type_constant t
                ON t.farm_activity_type_id = fa.farm_activity_type_id
            WHERE ff.farmer_id = :user_id
            ORDER BY fa.created_at DESC
            LIMIT :limit
            """
        ),
        {"user_id": str(user_id), "limit": _QUICK_REPLY_LIMIT},
    )
    return [
        ParentOption(id=str(r.id), label=_label(r.type_name or "กิจกรรม", r.created_at, r.farm_name))
        for r in rows
    ]


async def _harvest_choices(session: AsyncSession, user_id: UUID) -> list[ParentOption]:
    rows = await session.execute(
        text(
            """
            SELECT h.harvest_id AS id, h.harvest_date AS created_at,
                   COALESCE(f.farm_name, hb.hub_name, '') AS place
            FROM collection.harvest h
            LEFT JOIN agriculture.farm f ON f.farm_id = h.farm_id
            LEFT JOIN processing.hub hb ON hb.hub_id = h.hub_id
            WHERE h.farm_id IN (
                SELECT farm_id FROM agriculture.farmer_farm WHERE farmer_id = :user_id
            ) OR h.hub_id IN (
                SELECT hub_id FROM processing.hub_collector WHERE user_id = :user_id
            )
            ORDER BY h.harvest_date DESC
            LIMIT :limit
            """
        ),
        {"user_id": str(user_id), "limit": _QUICK_REPLY_LIMIT},
    )
    return [
        ParentOption(id=str(r.id), label=_label("เก็บเกี่ยว", r.created_at, r.place)) for r in rows
    ]


async def _batch_choices(session: AsyncSession, user_id: UUID) -> list[ParentOption]:
    rows = await session.execute(
        text(
            """
            SELECT b.batch_id AS id, b.created_at, ps.processing_station_name AS place
            FROM processing.batch b
            JOIN processing.processor_processing_station pps
                ON pps.processing_station_id = b.processing_station_id
            JOIN processing.processing_station ps
                ON ps.processing_station_id = b.processing_station_id
            WHERE pps.processor_id = :user_id
            ORDER BY b.created_at DESC
            LIMIT :limit
            """
        ),
        {"user_id": str(user_id), "limit": _QUICK_REPLY_LIMIT},
    )
    return [ParentOption(id=str(r.id), label=_label("แบทช์", r.created_at, r.place)) for r in rows]


_CHOICE_QUERY = {
    "farm_activity": _farm_activity_choices,
    "harvest": _harvest_choices,
    "batch": _batch_choices,
}


async def choices_for(session: AsyncSession, kind: str, user_id: UUID) -> list[ParentOption]:
    return await _CHOICE_QUERY[kind](session, user_id)
