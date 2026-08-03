from pydantic import BaseModel


class QuickReplyOption(BaseModel):
    """One tappable button under a message -- natural fit for guided-flow
    yes/no-style questions (GuidedFlow, target-architecture.md #4).
    """

    label: str
    text: str  # what gets "said" back to the bot when tapped
