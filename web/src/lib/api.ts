import {
  MissionRead,
  ControlStatusResponseType,
  AgentRead,
  TaskRead,
  AutoRunResponseType,
} from "./types";

const BASE = "/api/v1";

async function request<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`API ${res.status}: ${body}`);
  }
  return res.json() as Promise<T>;
}

// -- Missions --

export async function listMissions(): Promise<MissionRead[]> {
  return request<MissionRead[]>("/missions");
}

export async function getMission(id: string): Promise<MissionRead> {
  return request<MissionRead>(`/missions/${id}`);
}

export async function createControlMission(payload: {
  title: string;
  description?: string;
  source?: string;
}): Promise<MissionRead> {
  return request<MissionRead>("/control/missions", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

// -- Control actions --

export async function refineMission(
  id: string,
  prompt?: string,
): Promise<MissionRead> {
  return request<MissionRead>(`/control/missions/${id}/refine`, {
    method: "POST",
    body: JSON.stringify({ prompt }),
  });
}

export async function researchMission(
  id: string,
  query?: string,
): Promise<MissionRead> {
  return request<MissionRead>(`/control/missions/${id}/research`, {
    method: "POST",
    body: JSON.stringify({ query }),
  });
}

export async function verifyMission(
  id: string,
  prompt?: string,
): Promise<MissionRead> {
  return request<MissionRead>(`/control/missions/${id}/verify`, {
    method: "POST",
    body: JSON.stringify({ prompt }),
  });
}

export async function reviewPlan(
  id: string,
  prompt?: string,
): Promise<MissionRead> {
  return request<MissionRead>(`/control/missions/${id}/review-plan`, {
    method: "POST",
    body: JSON.stringify({ prompt }),
  });
}

export async function startExecution(id: string): Promise<MissionRead> {
  return request<MissionRead>(`/control/missions/${id}/start-execution`, {
    method: "POST",
  });
}

export async function autoRun(
  id: string,
  payload: {
    repo_url?: string;
    branch_prefix?: string;
    prompt?: string;
    agent_name?: string;
    auto_merge?: boolean;
    timeout_seconds?: number;
  } = {},
): Promise<AutoRunResponseType> {
  return request<AutoRunResponseType>(`/control/missions/${id}/auto-run`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function createBranch(id: string): Promise<{ branch: string }> {
  return request(`/control/missions/${id}/branch`, { method: "POST" });
}

export async function commitChanges(
  id: string,
  message?: string,
): Promise<{ commit_sha: string }> {
  return request(`/control/missions/${id}/commit`, {
    method: "POST",
    body: JSON.stringify({ message }),
  });
}

export async function mergeBranch(
  id: string,
  strategy = "squash",
): Promise<{ merge_sha: string }> {
  return request(`/control/missions/${id}/merge`, {
    method: "POST",
    body: JSON.stringify({ strategy }),
  });
}

// -- Control status --

export async function getControlStatus(
  id: string,
): Promise<ControlStatusResponseType> {
  return request<ControlStatusResponseType>(`/control/missions/${id}/status`);
}

// -- Agents --

export async function listAgents(
  missionId: string,
): Promise<AgentRead[]> {
  return request<AgentRead[]>(`/agents?mission_id=${missionId}`);
}

// -- Tasks --

export async function listTasks(
  missionId: string,
): Promise<TaskRead[]> {
  return request<TaskRead[]>(`/tasks?mission_id=${missionId}`);
}
