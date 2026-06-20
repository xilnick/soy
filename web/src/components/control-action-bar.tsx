"use client";

import { useState } from "react";
import {
  refineMission,
  researchMission,
  verifyMission,
  reviewPlan,
  startExecution,
  autoRun,
  createBranch,
  commitChanges,
  mergeBranch,
} from "@/lib/api";
import type { MissionRead } from "@/lib/types";
import { Button } from "./ui/button";

interface Props {
  mission: MissionRead;
  onAction: () => Promise<void>;
}

const ACTIONS = [
  { key: "refine", label: "Refine", states: ["created", "planning"] },
  { key: "research", label: "Research", states: ["created", "planning"] },
  { key: "verify", label: "Verify", states: ["created", "planning"] },
  { key: "review-plan", label: "Review Plan", states: ["created", "planning"] },
  { key: "start-execution", label: "Start Execution", states: ["created", "planning", "approved"] },
  { key: "auto-run", label: "Auto-Run", states: ["created", "planning", "approved"] },
  { key: "branch", label: "Create Branch", states: ["created", "planning"] },
  { key: "commit", label: "Commit", states: ["execution"] },
  { key: "merge", label: "Merge", states: ["execution", "reviewed"] },
] as const;

type ActionKey = (typeof ACTIONS)[number]["key"];

export function ControlActionBar({ mission, onAction }: Props) {
  const [busy, setBusy] = useState<ActionKey | null>(null);

  async function run(key: ActionKey) {
    setBusy(key);
    try {
      switch (key) {
        case "refine":
          await refineMission(mission.id);
          break;
        case "research":
          await researchMission(mission.id);
          break;
        case "verify":
          await verifyMission(mission.id);
          break;
        case "review-plan":
          await reviewPlan(mission.id);
          break;
        case "start-execution":
          await startExecution(mission.id);
          break;
        case "auto-run":
          await autoRun(mission.id);
          break;
        case "branch":
          await createBranch(mission.id);
          break;
        case "commit":
          await commitChanges(mission.id);
          break;
        case "merge":
          await mergeBranch(mission.id);
          break;
      }
      await onAction();
    } catch (err) {
      alert(err instanceof Error ? err.message : "Action failed");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="mb-6 flex flex-wrap gap-2">
      {ACTIONS.filter((a) => a.states.includes(mission.status as never)).map(
        (a) => (
          <Button
            key={a.key}
            size="sm"
            variant={a.key === "start-execution" || a.key === "auto-run" ? "default" : "secondary"}
            disabled={busy !== null}
            onClick={() => run(a.key)}
          >
            {busy === a.key ? `${a.label}...` : a.label}
          </Button>
        ),
      )}
    </div>
  );
}
