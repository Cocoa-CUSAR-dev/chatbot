from src.forms.schemas import FormDetail


def test_form_detail_defaults_to_no_sections() -> None:
    form = FormDetail(task_form_id="tf-1")
    assert form.sections == []


def test_form_detail_allows_extra_fields() -> None:
    # extra="allow" -- this mirrors whatever Kotlin's real response happens
    # to include beyond task_form_id/sections, without validation rejecting it.
    form = FormDetail.model_validate({"task_form_id": "tf-1", "title": "จดกิจกรรม"})
    assert form.model_extra is not None
    assert form.model_extra["title"] == "จดกิจกรรม"
