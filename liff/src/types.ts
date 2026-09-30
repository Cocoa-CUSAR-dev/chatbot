// Mirrors src/tasks/schemas.py's TaskListItem (chatbot repo) -- the
// service-key twin of mobile-backend's GetTasks, docs-and-plan#176.
export type TaskStatus = "NOT_STARTED" | "IN_PROGRESS" | "OVERDUE" | "COMPLETED";

export interface PendingTask {
  task_id: string;
  task_form_id: string;
  title: string;
  description: string | null;
  open_at: string | null;
  close_at: string | null;
  handler: string;
  is_multiple_submit: boolean;
  status: TaskStatus;
}
