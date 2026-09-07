from src.conversation.models import Conversation, ConversationAnswer


def test_conversation_table_schema_and_columns() -> None:
    table = Conversation.__table__
    assert table.schema == "chat"
    assert table.name == "conversation"
    assert set(table.c.keys()) == {
        "conversation_id",
        "user_id",
        "task_id",
        "task_form_id",
        "status",
        "current_question_id",
        "parent_answer",
    }
    assert table.c.current_question_id.nullable is True
    assert table.c.parent_answer.nullable is True


def test_conversation_foreign_keys_point_at_stub_tables() -> None:
    # These stub Tables exist purely for FK resolution (see models.py's own
    # docstring) -- this locks in that the FK strings actually resolve.
    fk_targets = {fk.target_fullname for fk in Conversation.__table__.foreign_keys}
    assert fk_targets == {"auth.user_account.user_id", "form.task.task_id"}


def test_conversation_answer_table_schema_and_columns() -> None:
    table = ConversationAnswer.__table__
    assert table.schema == "chat"
    assert table.name == "conversation_answer"
    assert set(table.c.keys()) == {
        "conversation_answer_id",
        "conversation_id",
        "question_id",
        "answer",
        "source",
    }


def test_conversation_answer_foreign_keys() -> None:
    fk_targets = {fk.target_fullname for fk in ConversationAnswer.__table__.foreign_keys}
    assert fk_targets == {"chat.conversation.conversation_id", "form.question.question_id"}
