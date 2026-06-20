"use client";

import { use, useCallback, useEffect, useState } from "react";
import { getMission, getControlStatus } from "@/lib/api";
import type { MissionRead, ControlStatusResponseType } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { ControlActionBar } from "@/components/control-action-bar";
import { AgentTeamList } from "@/components/agent-team-list";
import { TaskList } from "@/components/task-list";
import { GitInfoPanel } from "@/components/git-info-panel";
import { MetadataCard } from "@/components/metadata-card";
import { LiveEventStream } from "@/components/live-event-stream";

type Tab = "overview" | "agents" | "git" | "research" | "stream";

export default function MissionDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const [mission, setMission] = useState<MissionRead | null>(null);
  const [status, setStatus] = useState<ControlStatusResponseType | null>(null);
  const [tab, setTab] = useState<Tab>("overview");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [m, s] = await Promise.all([getMission(id), getControlStatus(id)]);
      setMission(m);
      setStatus(s);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load mission");
    }
  }, [id]);

  useEffect(() => {
    refresh().finally(() => setLoading(false));
  }, [refresh]);

  if (loading) return <p className="text-zinc-400">Loading mission...</p>;
  if (error)
    return (
      <p className="rounded-md border border-red-800 bg-red-950 p-3 text-sm text-red-200">
        {error}
      </p>
    );
  if (!mission) return <p className="text-zinc-400">Mission not found.</p>;

  const tabs: { key: Tab; label: string }[] = [
    { key: "overview", label: "Overview" },
    { key: "agents", label: "Agents & Tasks" },
    { key: "git", label: "Git" },
    { key: "research", label: "Research" },
    { key: "stream", label: "Live Stream" },
  ];

  return (
    <div>
      <div className="mb-6 flex items-center gap-3">
        <h1 className="text-2xl font-bold">{mission.title}</h1>
        <Badge label={mission.status} variant={mission.status} />
      </div>

      {mission.description && (
        <p className="mb-6 max-w-2xl text-sm text-zinc-400">
          {mission.description}
        </p>
      )}

      <ControlActionBar mission={mission} onAction={refresh} />

      <div className="mb-4 flex gap-1 border-b border-zinc-800">
        {tabs.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={`px-4 py-2 text-sm font-medium transition-colors ${
              tab === t.key
                ? "border-b-2 border-zinc-100 text-zinc-100"
                : "text-zinc-500 hover:text-zinc-300"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === "overview" && (
        <div className="grid gap-4 md:grid-cols-2">
          <Card title="Details">
            <dl className="space-y-2 text-sm">
              <div>
                <dt className="text-zinc-500">Status</dt>
                <dd className="text-zinc-200">{mission.status}</dd>
              </div>
              <div>
                <dt className="text-zinc-500">Source</dt>
                <dd className="text-zinc-200">{mission.source ?? "local"}</dd>
              </div>
              {mission.repo_url && (
                <div>
                  <dt className="text-zinc-500">Repo</dt>
                  <dd className="text-zinc-200">{mission.repo_url}</dd>
                </div>
              )}
              {mission.branch && (
                <div>
                  <dt className="text-zinc-500">Branch</dt>
                  <dd className="font-mono text-xs text-zinc-300">
                    {mission.branch}
                  </dd>
                </div>
              )}
            </dl>
          </Card>

          {status?.last_execution && (
            <Card title="Last Execution">
              <dl className="space-y-2 text-sm">
                <div>
                  <dt className="text-zinc-500">Status</dt>
                  <dd className="text-zinc-200">
                    {status.last_execution.status}
                  </dd>
                </div>
                {status.last_execution.started_at && (
                  <div>
                    <dt className="text-zinc-500">Started</dt>
                    <dd className="text-zinc-200">
                      {new Date(status.last_execution.started_at).toLocaleString()}
                    </dd>
                  </div>
                )}
                {status.last_execution.error && (
                  <div>
                    <dt className="text-zinc-500">Error</dt>
                    <dd className="text-red-300">
                      {status.last_execution.error}
                    </dd>
                  </div>
                )}
              </dl>
            </Card>
          )}

          {status && (
            <Card title="Progress">
              <dl className="space-y-2 text-sm">
                <div>
                  <dt className="text-zinc-500">Agents</dt>
                  <dd className="text-zinc-200">{status.agent_count}</dd>
                </div>
                <div>
                  <dt className="text-zinc-500">Tasks</dt>
                  <dd className="text-zinc-200">
                    {status.completed_tasks} / {status.task_count} completed
                  </dd>
                </div>
              </dl>
              {status.task_count > 0 && (
                <div className="mt-3 h-2 w-full overflow-hidden rounded-full bg-zinc-800">
                  <div
                    className="h-full rounded-full bg-zinc-400 transition-all"
                    style={{
                      width: `${Math.round((status.completed_tasks / status.task_count) * 100)}%`,
                    }}
                  />
                </div>
              )}
            </Card>
          )}
        </div>
      )}

      {tab === "agents" && (
        <div className="space-y-4">
          <AgentTeamList missionId={id} />
          <TaskList missionId={id} />
        </div>
      )}

      {tab === "git" && (
        <GitInfoPanel
          gitInfo={status?.git_info ?? null}
          branch={mission.branch}
        />
      )}

      {tab === "research" && (
        <div className="space-y-4">
          <MetadataCard
            title="Research Results"
            items={status?.research_results ?? []}
            emptyLabel="No research results yet. Use the Research action above."
          />
          <MetadataCard
            title="Verification"
            items={status?.verification_results ?? []}
            emptyLabel="No verification results yet."
          />
          <MetadataCard
            title="Refinement History"
            items={status?.refinement_history ?? []}
            emptyLabel="No refinements yet."
          />
        </div>
      )}

      {tab === "stream" && <LiveEventStream missionId={id} />}
    </div>
  );
}
