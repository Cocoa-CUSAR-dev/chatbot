# Cocoa Chatbot Service

The LINE OA AI chatbot for the Cocoa Supply Chain Databank (Is Thai Cacao) — the new Phase I work that lets farmers submit data by chatting in LINE instead of only through the mobile app. See [AGENTS.md](./AGENTS.md) for the same conventions in a terse, AI-agent-facing format.

## Role in the plan

This service sits on the **seam** between the new work and the two existing backends — it doesn't replace either of them:

- Reads form structure from Kotlin's existing `GET /forms/{formId}` (`src/forms`)
- Writes farmer answers via Go's existing `POST /tasks` (`src/tasks`)
- Owns its **own** new database schemas directly (`chat.*`, `notify.*`, and whatever identity-linking lands on) — see the `database` repo for the actual Flyway migrations; this service never runs migrations itself

Full reasoning: [ADR 0001](../cocoa-docs/docs/adr/0001-old-new-integration-seam.md) (the seam), [ADR 0003](../cocoa-docs/docs/adr/0003-chatbot-service-stack.md) (this stack), [ADR 0004](../cocoa-docs/docs/adr/0004-llm-extraction-approach.md) (LLM approach), [ADR 0006](../cocoa-docs/docs/adr/0006-reminder-delivery.md) (reminders). Diagrams: `cocoa-docs`'s [Target Architecture](../cocoa-docs/docs/architecture/target-architecture.md) page.

**ADR 0002 (identity linking) is reopened, still being decided by the team.** There is deliberately no `src/identity` (or equivalent) module yet — the pairing-code approach it originally described was scaffolded and then removed once the ADR reopened, rather than left in place as stale code implying a decision that isn't final. Whatever mechanism the team lands on gets a fresh module once it's settled, not a revival of the old one.

## Tech stack

- **FastAPI** — async-native, matches the "ack the webhook fast, process after" pattern LINE requires
- **`line-bot-sdk`** — official SDK, webhook signature verification + LIFF
- **LiteLLM** — provider-agnostic LLM calls; switching models is a config string, not code (ADR 0004)
- **APScheduler** — the reminder push job and the LLM-retry cron, in-process, no separate broker
- **SQLAlchemy (async) + asyncpg** — for this service's own new schemas only
- **Pydantic** — shared schema definitions, reused for both Go-submission validation and LLM structured output

## Project structure

Domain-driven, one folder per capability under `src/` — not by file type. Each domain folder holds whichever of `router.py` / `schemas.py` / `models.py` / `service.py` / `client.py` / `dependencies.py` / `config.py` / `constants.py` / `exceptions.py` it actually needs, not all of them by default.

```
src/
├── line/          # webhook signature verification, reply/push sending -- ALL direct LINE API/SDK usage lives here
├── conversation/    # the state machine / slot-filling engine (target-architecture.md #4)
├── llm/              # LiteLLM wrapper -- extraction AND follow-up question generation
├── forms/             # read-only proxy -> Kotlin GET /forms/{formId}
├── tasks/              # write proxy -> Go POST /tasks
├── reminders/           # APScheduler jobs
├── config.py, database.py, models.py, exceptions.py   # global, shared
└── main.py               # FastAPI app, routers, lifespan (starts/stops the scheduler)
tests/                     # mirrors src/'s structure
liff/                       # placeholder for the separate Vite+React LIFF app
```

One deliberate deviation from common FastAPI convention worth flagging: **no Alembic**. Migrations for this service's own tables are still owned by the `database` repo's Flyway setup (ADR 0005) — same single-migration-history reasoning as the existing Go/Kotlin backends, just extended to this service too.

## First-time setup

```bash
cp .env.sample .env   # fill in real values
python -m venv .venv && source .venv/Scripts/activate   # or .venv/bin/activate on macOS/Linux
pip install -e ".[dev]"
uvicorn src.main:app --reload
```

`.env.sample` covers three things: the database connection, the two existing backends' base URLs, and LINE's channel credentials — see the file itself for what each one is for and why the URL format differs from what Go/Kotlin use (SQLAlchemy needs `postgresql+asyncpg://`, not the plain `postgresql://` used elsewhere).

## Running tests

```bash
pytest
```

`tests/conftest.py` sets dummy env vars before anything under `src` gets imported (every `config.py` in this project builds its `Settings` object at import time) — tests run cleanly with no real `.env` needed.

## CI

`.github/workflows/ci.yml` runs on every push: lint (ruff) → type-check (mypy) → test (the full suite under `tests/`, every time, not path-filtered) → build (Docker). Matches the pipeline shape [ADR 0007](../cocoa-docs/docs/adr/0007-deployment-and-hosting.md) already decided — deploy target is still pending there, so there's no deploy stage yet.

## What's actually implemented vs. scaffolded

This is a **structure-first** pass, verified to actually import and run (all tests pass, real `line-bot-sdk`/`litellm`/`apscheduler` APIs were checked against the installed packages, not guessed from memory) — but the real business logic inside each module is mostly a thin, honest skeleton with `TODO`s, not a finished feature:

- `src/line`: webhook signature verification and reply/push sending are real and tested. The event dispatcher just echoes back the message text — it doesn't call `src/conversation` yet.
- `src/conversation`: the state-transition *rules* are real and tested (`state_machine.py`) — matches the corrected two-loop model from `target-architecture.md`. The orchestration that actually drives a conversation turn-by-turn isn't wired up yet.
- `src/forms` / `src/tasks`: real HTTP clients against the shapes ADR 0001 describes — untested against the real Kotlin/Go endpoints yet.
- `src/reminders`: the scheduler wiring is real; the job body is a stub (`jobs.py`).

None of the ORM models here create or alter any table — they describe tables the `database` repo's Flyway migrations are expected to create (ADR 0005). Until those migrations actually land, none of this can run against a real database.
