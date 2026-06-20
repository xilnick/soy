"use client";

import { useEffect, useState } from "react";
import { listAgents } from "@/lib/api";
import type { AgentRead } from "@/lib/types";
import { Card } from "./ui/card";
import { Badge } from "./ui/badge";

export function AgentTeamList({ missionId }: { missionId: string }) {
  const [agents, setAgents] = useState<AgentRead[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    listAgents(missionId)
      .then(setAgents)
      .finally(() => setLoading(false));
  }, [missionId]);

  if (loading) return <p className="text-sm text-zinc-500">Loading agents...</p>;

  if (agents.length === 0) {
    return (
      <Card>
        <p className="text-sm text-zinc-500">
          No agents assigned yet. Agents are created when the mission enters
          execution.
        </p>
      </Card>
    );
  }

  return (
    <Card title="Agent Team">
      <div className="space-y-3">
        {agents.map((a) => (
          <div
            key={a.id}
            className="flex items-center justify-between rounded-md border border-zinc-800 bg-zinc-950 px-3 py-2"
          >
            <div>
              <span className="text-sm font-medium text-zinc-200">
                {a.name}
              </span>
              <span className="ml-2 text-xs text-zinc-500">{a.role}</span>
            </div>
            <div className="flex items-center gap-2">
              {a.model && (
                <span className="font-mono text-xs text-zinc-600">
                  {a.model}
                </span>
              )}
              <Badge label={a.status} />
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}
