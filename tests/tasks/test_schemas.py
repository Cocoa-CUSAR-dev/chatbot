from src.tasks.schemas import TaskSubmission


def test_task_submission_round_trip() -> None:
    submission = TaskSubmission(user_id="u-1", task_id="t-1", answer={"farm_id": "f-1"})
    assert submission.model_dump() == {
        "user_id": "u-1",
        "task_id": "t-1",
        "answer": {"farm_id": "f-1"},
    }
