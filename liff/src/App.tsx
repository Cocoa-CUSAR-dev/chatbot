import { useCallback, useEffect, useState } from "react";
import "./App.css";
import { fetchPendingTasks, startTask } from "./api";
import {
  AlertIcon,
  ArrowRightIcon,
  CalendarIcon,
  CircleDotIcon,
  ProgressIcon,
  SproutIcon,
} from "./icons";
import { closeLiffWindow, ensureLiffReady } from "./liffClient";
import type { PendingTask, TaskStatus } from "./types";

type LoadState =
  | { phase: "loading" }
  | { phase: "error"; message: string }
  | { phase: "ready"; tasks: PendingTask[] };

const STATUS_META: Record<
  TaskStatus,
  { label: string; badgeClass: string; icon: typeof CircleDotIcon }
> = {
  NOT_STARTED: { label: "ยังไม่เริ่ม", badgeClass: "badge--not-started", icon: CircleDotIcon },
  IN_PROGRESS: { label: "กำลังทำ", badgeClass: "badge--in-progress", icon: ProgressIcon },
  OVERDUE: { label: "เลยกำหนดแล้ว", badgeClass: "badge--overdue", icon: AlertIcon },
  // Server already filters COMPLETED out of this list; kept for type
  // completeness rather than assuming it can never appear.
  COMPLETED: { label: "เสร็จแล้ว", badgeClass: "badge--not-started", icon: CircleDotIcon },
};

function formatCloseAt(closeAt: string | null): string | null {
  if (!closeAt) return null;
  const date = new Date(closeAt);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleDateString("th-TH", { day: "numeric", month: "short", year: "numeric" });
}

function TaskCardSkeleton({ delay }: { delay: number }) {
  return (
    <div className="skeleton-card" style={{ animationDelay: `${delay}ms` }}>
      <div className="skeleton-line" style={{ width: "70%", marginBottom: "0.6rem" }} />
      <div className="skeleton-line" style={{ width: "40%", height: "0.75rem" }} />
    </div>
  );
}

function TaskCard({
  task,
  index,
  onOpen,
  opening,
}: {
  task: PendingTask;
  index: number;
  onOpen: (task: PendingTask) => void;
  opening: boolean;
}) {
  const meta = STATUS_META[task.status];
  const StatusIcon = meta.icon;
  const closeAtLabel = formatCloseAt(task.close_at);

  return (
    <div className="card" style={{ animationDelay: `${index * 60}ms` }}>
      <div className="card-top">
        <p className="card-title">{task.title}</p>
        <span className={`badge ${meta.badgeClass}`}>
          <StatusIcon aria-hidden />
          {meta.label}
        </span>
      </div>
      {closeAtLabel && (
        <div className={`due-row ${task.status === "OVERDUE" ? "due-row--overdue" : ""}`}>
          <CalendarIcon aria-hidden />
          กำหนดส่ง {closeAtLabel}
        </div>
      )}
      <button className="open-button" onClick={() => onOpen(task)} disabled={opening}>
        {opening ? (
          <>
            <ProgressIcon className="spinner" aria-hidden />
            กำลังเปิด...
          </>
        ) : (
          <>
            เปิด
            <ArrowRightIcon aria-hidden />
          </>
        )}
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

  return (
    <div className="page">
      <div className="header">
        <h1>งานที่ต้องทำ</h1>
        {state.phase === "ready" && (
          <span className="count">{state.tasks.length} รายการ</span>
        )}
      </div>

      {state.phase === "loading" && (
        <>
          <TaskCardSkeleton delay={0} />
          <TaskCardSkeleton delay={80} />
          <TaskCardSkeleton delay={160} />
        </>
      )}

      {state.phase === "error" && (
        <div className="center-state error">
          <div className="icon-wrap">
            <AlertIcon aria-hidden />
          </div>
          <p>{state.message}</p>
          <button className="retry-button" onClick={load}>
            ลองใหม่
          </button>
        </div>
      )}

      {state.phase === "ready" && state.tasks.length === 0 && (
        <div className="center-state empty">
          <div className="icon-wrap">
            <SproutIcon aria-hidden />
          </div>
          <p>
            ไม่มีงานที่ต้องทำในตอนนี้
            <br />
            พักได้เลย 🌱
          </p>
        </div>
      )}

      {state.phase === "ready" &&
        state.tasks.map((task, index) => (
          <TaskCard
            key={task.task_id}
            task={task}
            index={index}
            onOpen={handleOpen}
            opening={openingTaskId === task.task_id}
          />
        ))}
    </div>
  );
}

export default App;
