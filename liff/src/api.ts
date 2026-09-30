import { getAuthHeader } from "./liffClient";
import type { PendingTask } from "./types";

const API_BASE = import.meta.env.VITE_API_BASE_URL;

function apiBase(): string {
  if (!API_BASE) {
    throw new Error("VITE_API_BASE_URL is not set -- see .env.sample");
  }
  return API_BASE;
}

export async function fetchPendingTasks(): Promise<PendingTask[]> {
  const response = await fetch(`${apiBase()}/line/liff/tasks`, {
    headers: getAuthHeader(),
  });
  if (!response.ok) {
    throw new Error(`GET /line/liff/tasks failed: ${response.status}`);
  }
  return (await response.json()) as PendingTask[];
}

// Fire-and-forget from the caller's point of view: the actual conversation
// content arrives as a LINE push message, not in this response (see
// src/line/liff_tasks.py's start_task) -- the caller's job after this
// resolves is just closeLiffWindow(), not rendering anything from it.
export async function startTask(task: PendingTask): Promise<void> {
  const response = await fetch(`${apiBase()}/line/liff/tasks/${task.task_id}/start`, {
    method: "POST",
    headers: {
      ...getAuthHeader(),
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      task_form_id: task.task_form_id,
      handler: task.handler,
    }),
  });
  if (!response.ok) {
    throw new Error(`POST /line/liff/tasks/${task.task_id}/start failed: ${response.status}`);
  }
}
