"""LLM fallback for free text the fixed parsers in text_parsing.py couldn't
handle -- INT/FLOAT/DATE/DATETIME only. OPTION/BOOLEAN never come here: those
are buttons, so an unmatched typed answer is simply re-asked with the same
buttons (see service.py's constrained-choices branch).

The LLM only proposes a normalized string; service.py still runs it through
validate_answer like any other answer, so a wrong guess is rejected by the
field's own rule instead of being stored.

Never raises: no API key, provider failure/timeout, or a null answer all
return None, and the caller falls back to the farmer's original text exactly
as if this module didn't exist.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from src.llm.client import extract_slots
from src.llm.config import llm_settings
from src.llm.exceptions import LLMUnavailable

logger = logging.getLogger(__name__)

_BANGKOK = ZoneInfo("Asia/Bangkok")

_TYPE_INSTRUCTIONS = {
    "INT": "a whole number written with ASCII digits only, e.g. 900",
    "FLOAT": "a number written with ASCII digits and an optional decimal point, e.g. 12.5",
    "DATE": "a calendar date as ISO 8601 YYYY-MM-DD in the Gregorian calendar",
    "DATETIME": "a date and time as ISO 8601 YYYY-MM-DDTHH:MM:SS in the Gregorian calendar",
}


class _ExtractedValue(BaseModel):
    value: str | None = None


async def try_llm_parse(rule_type: str, raw_text: str) -> str | None:
    if not llm_settings.LLM_API_KEY or rule_type not in _TYPE_INSTRUCTIONS:
        return None

    today = datetime.now(_BANGKOK).date().isoformat()
    instructions = (
        "A Thai farmer typed a free-text answer to a form question. "
        f"Extract it as {_TYPE_INSTRUCTIONS[rule_type]}. "
        "Thai Buddhist Era years (พ.ศ.) must be converted to Gregorian by subtracting 543. "
        f"Today is {today} (Asia/Bangkok) for resolving relative words like วันนี้ or เมื่อวาน. "
        "Do not guess: if the text does not clearly contain such a value, return null. "
        "Return only the value, with no units or extra words."
    )
    try:
        result = await extract_slots(raw_text, _ExtractedValue, instructions=instructions)
    except LLMUnavailable:
        logger.warning("llm parse unavailable rule_type=%s", rule_type)
        return None
    except Exception:  # noqa: BLE001 -- a malformed model response must not break the answer flow
        logger.warning("llm parse returned an unusable response rule_type=%s", rule_type)
        return None

    value = (result.value or "").strip()
    return value or None
