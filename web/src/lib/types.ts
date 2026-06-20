import { z } from "zod";

// -- Enums (mirrors soy.models.enums) --

export const MissionStatus = z.enum([
  "created",
  "planning",
  "approved",
  "execution",
  "reviewed",
  "merged",
  "rejected",
  "escalated",
]);
export type MissionStatus = z.infer<typeof MissionStatus>;

// -- Mission --

export const MissionRead = z.object({
  id: z.string().uuid(),
  title: z.string(),
  description: z.string().nullable().optional(),
  status: MissionStatus,
  source: z.string().nullable().optional(),
  repo_url: z.string().nullable().optional(),
  branch_prefix: z.string().nullable().optional(),
  branch: z.string().nullable().optional(),
  metadata: z.record(z.unknown()).default({}),
});
export type MissionRead = z.infer<typeof MissionRead>;

export const ControlMissionCreate = z.object({
  title: z.string().min(1).max(512),
  description: z.string().max(10000).optional(),
  source: z.string().max(64).optional(),
  mission_metadata: z.record(z.unknown()).optional(),
});

export const AutoRunRequest = z.object({
  repo_url: z.string().optional(),
  branch_prefix: z.string().optional(),
  prompt: z.string().optional(),
  agent_name: z.string().optional(),
  auto_merge: z.boolean().default(false),
  timeout_seconds: z.number().optional(),
});

export const AutoRunResponse = z.object({
  mission_id: z.string().uuid(),
  status: z.string(),
  branch: z.string().optional(),
  commit_sha: z.string().optional(),
  merged: z.boolean().optional(),
  merge_sha: z.string().optional(),
  agent_output: z.unknown().optional(),
  message: z.string().optional(),
  error: z.string().optional(),
});
export type AutoRunResponseType = z.infer<typeof AutoRunResponse>;

export const ControlStatusResponse = z.object({
  mission_id: z.string().uuid(),
  title: z.string(),
  status: MissionStatus,
  description: z.string().nullable().optional(),
  repo_url: z.string().nullable().optional(),
  branch: z.string().nullable().optional(),
  agent_count: z.number(),
  task_count: z.number(),
  completed_tasks: z.number(),
  git_info: z.record(z.unknown()).nullable().optional(),
  research_results: z.array(z.record(z.unknown())).nullable().optional(),
  verification_results: z.array(z.record(z.unknown())).nullable().optional(),
  refinement_history: z.array(z.record(z.unknown())).nullable().optional(),
  last_execution: z
    .object({
      id: z.string(),
      status: z.string(),
      started_at: z.string().nullable().optional(),
      finished_at: z.string().nullable().optional(),
      error: z.string().nullable().optional(),
    })
    .nullable()
    .optional(),
});
export type ControlStatusResponseType = z.infer<typeof ControlStatusResponse>;

// -- Agent --

export const AgentRead = z.object({
  id: z.string().uuid(),
  mission_id: z.string().uuid(),
  name: z.string(),
  role: z.string(),
  status: z.string(),
  model: z.string().nullable().optional(),
});
export type AgentRead = z.infer<typeof AgentRead>;

// -- Task --

export const TaskRead = z.object({
  id: z.string().uuid(),
  mission_id: z.string().uuid(),
  agent_id: z.string().uuid().nullable().optional(),
  description: z.string(),
  status: z.string(),
  attempt_count: z.number(),
  depends_on: z.array(z.string().uuid()).default([]),
});
export type TaskRead = z.infer<typeof TaskRead>;

// -- WebSocket events --

export const WsEvent = z.object({
  type: z.string(),
  payload: z.record(z.unknown()),
  timestamp: z.string(),
});
export type WsEvent = z.infer<typeof WsEvent>;
