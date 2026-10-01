# LIFF app

The farmer-facing to-do list screen (docs-and-plan#176, US4-2) -- a real
LIFF screen replacing `src/line/temp_task_picker.py`'s keyword-triggered
Quick Reply fallback. Per [ADR 0003](../README.md#related-adrs), this is a
**separate, lightweight Vite + React app**, not folded into the FastAPI
backend or the existing Next.js `web-app`.

## What it does

1. Initializes LIFF, logs the farmer in if needed (`src/liffClient.ts`).
2. `GET /line/liff/tasks` (the chatbot backend, `src/line/liff_tasks.py`) --
   lists pending tasks with due status.
3. Tapping "เปิด" on a task calls `POST /line/liff/tasks/{task_id}/start`,
   then closes the LIFF window (`liff.closeWindow()`). The actual
   conversation content (a question, a confirmation, whatever it
   resumes/starts at) arrives as a LINE **push** message in the chat the
   farmer lands back on -- not rendered in this app.

## Running locally

```bash
npm install
cp .env.sample .env
# fill in VITE_LIFF_ID and VITE_API_BASE_URL, see .env.sample
npm run dev
```

Outside LINE's in-app browser (`liff.isInClient()` false), `closeLiffWindow`
is a no-op instead of throwing, so the task list and "เปิด" click-through
can be exercised in a plain desktop browser -- but `liff.login()` still
needs a real LIFF ID to redirect through, and the pushed follow-up message
only shows up in the real LINE chat, not in this app.

## Deploying

Whoever registers the LIFF app in the LINE Developers Console (under the
chatbot's Messaging API channel, "LIFF" tab -- a manual, one-time console
step this repo's code can't do for you) needs to set its Endpoint URL to
wherever this is deployed, and the chatbot backend's `CORS_ORIGINS` needs
that same origin.
