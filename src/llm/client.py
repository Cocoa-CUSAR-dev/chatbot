"""LiteLLM wrapper -- the LLM has two distinct jobs here, not one (see the
`LLMConversation` state machine in target-architecture.md #4):
  1. extract_slots() -- pull structured fields out of the farmer's free text
  2. generate_follow_up() -- author the next question in natural language
     when some required slots are still missing

Both go through this one client so the provider/model is swappable in one
place (ADR 0004).
"""

import litellm
from pydantic import BaseModel

from src.llm.config import llm_settings
from src.llm.exceptions import LLMUnavailable


async def extract_slots[SchemaT: BaseModel](text: str, schema: type[SchemaT]) -> SchemaT:
    """Extract whatever `schema`'s fields can be found in `text`.

    Fields the model can't find should come back unset (None) on the schema,
    not raise -- "some slots missing" is a normal, successful result. Only
    genuine provider failure/timeout should raise LLMUnavailable.
    """
    try:
        response = await litellm.acompletion(
            model=llm_settings.LLM_MODEL,
            api_key=llm_settings.LLM_API_KEY,
            messages=[{"role": "user", "content": text}],
            response_format=schema,
        )
    except Exception as exc:  # noqa: BLE001 -- provider errors are intentionally broad here
        raise LLMUnavailable from exc

    content = response.choices[0].message.content
    return schema.model_validate_json(content)


async def generate_follow_up(missing_field: str, context: str) -> str:
    """Ask the LLM to author a natural-language question for one missing slot.

    This is the piece that fixes the earlier bug in this design: the guided
    fallback used to just re-ask a static question in a bare loop. When the
    LLM is healthy, it composes the question instead -- see GuidedFlow vs.
    LLMConversation in target-architecture.md #4.
    """
    prompt = (
        f"Given this conversation so far:\n{context}\n\n"
        f"Write one short, natural follow-up question in Thai asking the farmer "
        f"for: {missing_field}. Just the question, nothing else."
    )
    try:
        response = await litellm.acompletion(
            model=llm_settings.LLM_MODEL,
            api_key=llm_settings.LLM_API_KEY,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001
        raise LLMUnavailable from exc

    return response.choices[0].message.content
