"use client";

import { useEffect, useState } from "react";
import { listTasks } from "@/lib/api";
import type { TaskRead } from "@/lib/types";
import { Card } from "./ui/card";
import { Badge } from "./ui/badge";

const taskVariant: Record<string, string> = {
  pending: "created",
  in_progress: "execution",
  completed: "merged",
  failed: "rejected",
  escalated: "escalated",
};

export function TaskList({ missionId }: { missionId: string }) {
  const [tasks, setTasks] = useState<TaskRead[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    listTasks(missionId)
      .then(setTasks)
      .finally(() => setLoading(false));
  }, [missionId]);

  if (loading) return <p className="text-sm text-zinc-500">Loading tasks...</p>;

  if (tasks.length === 0) {
    return (
      <Card>
        <p className="text-sm text-zinc-500">
          No tasks yet. Tasks are created during the planning phase.
        </p>
      </Card>
    );
  }

  return (
    <Card title="Tasks">
      <div className="space-y-2">
        {tasks.map((t) => (
          <div
            key={t.id}
            className="flex items-start justify-between gap-3 rounded-md border border-zinc-800 bg-zinc-950 px-3 py-2"
          >
            <p className="flex-1 text-sm text-zinc-300">{t.description}</p>
            <div className="flex shrink-0 items-center gap-2">
              {t.attempt_count > 0 && (
                <span className="text-xs text-zinc-600">
                  x{t.attempt_count}
                </span>
              )}
              <Badge label={t.status} variant={taskVariant[t.status]} />
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}
