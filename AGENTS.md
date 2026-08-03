# AGENTS.md

Terse, machine-readable version of this repo's conventions. Full prose: [README.md](./README.md). Full architecture reasoning: the `cocoa-docs` repo's ADRs (linked inline below).

## Version matrix

| Package | Version | Notes |
|---|---|---|
| Python | >=3.12 | |
| fastapi | >=0.115 | |
| line-bot-sdk | >=3.14 | v3 API (`linebot.v3.*`) — not the old v2 `LineBotApi` |
| litellm | >=1.52 | |
| apscheduler | >=3.10 | `AsyncIOScheduler` |
| sqlalchemy | >=2.0 | async engine, `Mapped`/`mapped_column` style |
| asyncpg | >=0.30 | driver — URLs use `postgresql+asyncpg://` |

## Do

- Put new capabilities in their own `src/<domain>/` folder — router/schemas/models/service/client/dependencies/config/constants/exceptions, only the ones that domain actually needs.
- Name every table's PK `<entity>_id` (e.g. `conversation_id`, `line_identity_id`) via `src/models.py`'s `uuid_pk()` helper — **never** a generic `id`. This matches every real table in the shared database; a generic `id` will not match the actual schema.
- Reuse `src.exceptions.ServiceException` as the base for any new domain exception; register new ones via `@app.exception_handler` in `main.py` only if they need special handling beyond the base's status_code/detail.
- Keep ORM models in `src/<domain>/models.py` describing tables the `database` repo's Flyway migrations own — this service never creates or alters schema itself.
- When adding an LLM call, go through `src/llm/client.py` — one place, one config (`LLM_MODEL`/`LLM_API_KEY`), swappable without touching call sites.
- When adding a scheduled job, register it in `src/reminders/scheduler.py` and put the body in `jobs.py` — keep job bodies plain async functions, testable without APScheduler in the loop.
- Set required env vars in `tests/conftest.py` (via `os.environ.setdefault`) before any `src` import — every `config.py` builds its `Settings()` at import time; forgetting this breaks test collection with a confusing `ValidationError`, not a clear message.

## Don't

- Don't add Alembic, or any other migration tool, to this repo. Migrations are the `database` repo's job (ADR 0005) — this is a deliberate, decided constraint, not an oversight.
- Don't call Kotlin or Go directly from a router — go through `src/forms/client.py` or `src/tasks/client.py`. They're the seam (ADR 0001); adding a second ad-hoc HTTP call site duplicates logic that's supposed to live in one place.
- Don't treat "some required slots still missing" as a trigger to fall back to `GuidedFlow`. Only genuine LLM failure/timeout does — see `src/conversation/state_machine.py` and its own tests (`tests/conversation/test_state_machine.py`) for the exact rule this repo already got wrong twice before landing on.
- Don't assume the switch between `LLMConversation` and `GuidedFlow` is one-way. It's bidirectional and can fire mid-loop (`on_llm_recovered` exists for a reason) — see `target-architecture.md #4` in `cocoa-docs`.
- Don't use `BackgroundTasks` for anything where losing the task silently would page someone. If it needs retries, visibility, or scheduling, it belongs in `src/reminders` (APScheduler), not a fire-and-forget background task.
- Don't hand-roll LINE webhook signature verification. `src/line/dependencies.py`'s `parse_line_events` already does it via `WebhookParser` — this is the whole reason `line-bot-sdk` was chosen (ADR 0003).

## Anti-patterns checklist (review before merging)

- [ ] New table/column added without a corresponding Flyway migration in the `database` repo (schema drift is exactly what Flyway was adopted to prevent — `GO-1`)
- [ ] New PK column named `id` instead of `<entity>_id`
- [ ] A route handler that both talks to Kotlin/Go **and** contains conversation-state logic (should be split: client call in `forms`/`tasks`, state logic in `conversation`)
- [ ] A test that imports `src.main` (or anything that transitively imports a `config.py`) without `tests/conftest.py`'s env vars already set
- [ ] A `sync` def route or dependency doing real I/O — should be `async def` (ADR 3.3-equivalent: FastAPI's async-first design, see README's linked FastAPI best-practices doc)
- [ ] Provider-specific LLM code outside `src/llm` (defeats the point of LiteLLM abstraction)
