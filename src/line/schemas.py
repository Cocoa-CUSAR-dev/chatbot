from typing import Literal

from pydantic import BaseModel


class QuickReplyOption(BaseModel):
    """One tappable button under a message -- natural fit for guided-flow
    yes/no-style questions (GuidedFlow, target-architecture.md #4).

    `kind` picks the LINE action behind the button. "message" (the default,
    and every button this service sent before GEODATA support) re-sends
    `text` as if the farmer typed it. "location" opens LINE's own native
    location picker inside the app -- no browser, no LIFF -- and the farmer's
    choice comes back as a location message, not text; `text` is unused for
    it, since LINE has nothing to "say" on the farmer's behalf there.
    """

    label: str
    text: str = ""  # what gets "said" back to the bot when tapped (kind="message")
    kind: Literal["message", "location"] = "message"
