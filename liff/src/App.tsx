import { useCallback, useEffect, useState } from "react";
import { fetchPendingTasks, startTask } from "./api";
import { closeLiffWindow, ensureLiffReady } from "./liffClient";
import type { PendingTask, TaskStatus } from "./types";

type LoadState =
  | { phase: "loading" }
  | { phase: "error"; message: string }
  | { phase: "ready"; tasks: PendingTask[] };

const STATUS_LABEL: Record<TaskStatus, string> = {
  NOT_STARTED: "ยังไม่เริ่ม",
  IN_PROGRESS: "กำลังทำ",
  OVERDUE: "เลยกำหนดแล้ว",
  COMPLETED: "เสร็จแล้ว", // server already filters this out; kept for completeness
};

function formatCloseAt(closeAt: string | null): string | null {
  if (!closeAt) return null;
  const date = new Date(closeAt);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleDateString("th-TH", { day: "numeric", month: "short", year: "numeric" });
}

function TaskCard({
  task,
  onOpen,
  opening,
}: {
  task: PendingTask;
  onOpen: (task: PendingTask) => void;
  opening: boolean;
}) {
  const closeAtLabel = formatCloseAt(task.close_at);
  return (
    <div
      style={{
        background: "var(--card-bg)",
        border: "1px solid var(--border)",
        borderRadius: "0.75rem",
        padding: "1rem",
        marginBottom: "0.75rem",
        boxShadow: "var(--shadow)",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", gap: "0.5rem" }}>
        <strong style={{ fontSize: "1.05rem" }}>{task.title}</strong>
        <span
          style={{
            fontSize: "0.75rem",
            whiteSpace: "nowrap",
            color: task.status === "OVERDUE" ? "var(--overdue)" : "var(--text-muted)",
            fontWeight: task.status === "OVERDUE" ? 600 : 400,
          }}
        >
          {STATUS_LABEL[task.status]}
        </span>
      </div>
      {closeAtLabel && (
        <p style={{ color: "var(--text-muted)", fontSize: "0.875rem", margin: "0.25rem 0 0" }}>
          กำหนดส่ง: {closeAtLabel}
        </p>
      )}
      <button
        onClick={() => onOpen(task)}
        disabled={opening}
        style={{
          marginTop: "0.75rem",
          width: "100%",
          padding: "0.625rem",
          borderRadius: "0.5rem",
          border: "none",
          background: "var(--accent)",
          color: "#fff",
          fontSize: "1rem",
          fontWeight: 600,
          cursor: opening ? "default" : "pointer",
          opacity: opening ? 0.6 : 1,
        }}
      >
        {opening ? "กำลังเปิด..." : "เปิด"}
      </button>
    </div>
  );
}

function App() {
  const [state, setState] = useState<LoadState>({ phase: "loading" });
  const [openingTaskId, setOpeningTaskId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setState({ phase: "loading" });
    try {
      await ensureLiffReady();
      const tasks = await fetchPendingTasks();
      setState({ phase: "ready", tasks });
    } catch (error) {
      setState({
        phase: "error",
        message: error instanceof Error ? error.message : "เกิดข้อผิดพลาดที่ไม่ทราบสาเหตุ",
      });
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const handleOpen = useCallback(async (task: PendingTask) => {
    setOpeningTaskId(task.task_id);
    try {
      await startTask(task);
      // The real content (a question, a confirmation, whatever the
      // conversation resumes/starts at) arrives as a LINE push message,
      // not in this response -- see src/line/liff_tasks.py's start_task.
      // Closing here is what actually shows it to the farmer.
      closeLiffWindow();
    } catch (error) {
      setOpeningTaskId(null);
      setState({
        phase: "error",
        message: error instanceof Error ? error.message : "เปิดงานนี้ไม่สำเร็จ กรุณาลองใหม่",
      });
    }
  }, []);

  if (state.phase === "loading") {
    return <p style={{ textAlign: "center", marginTop: "2rem" }}>กำลังโหลด...</p>;
  }

  if (state.phase === "error") {
    return (
      <div style={{ textAlign: "center", marginTop: "2rem" }}>
        <p style={{ color: "var(--overdue)" }}>{state.message}</p>
        <button onClick={load} style={{ marginTop: "0.5rem" }}>
          ลองใหม่
        </button>
      </div>
    );
  }

  if (state.tasks.length === 0) {
    return <p style={{ textAlign: "center", marginTop: "2rem" }}>ไม่มีงานที่ต้องทำในตอนนี้ 🎉</p>;
  }

  return (
    <div>
      <h1 style={{ fontSize: "1.25rem", margin: "0 0 1rem" }}>งานที่ต้องทำ</h1>
      {state.tasks.map((task) => (
        <TaskCard
          key={task.task_id}
          task={task}
          onOpen={handleOpen}
          opening={openingTaskId === task.task_id}
        />
      ))}
    </div>
  );
}

export default App;
