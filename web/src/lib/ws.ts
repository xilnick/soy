import { WsEvent } from "./types";

const WS_BASE =
  typeof window !== "undefined"
    ? `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}`
    : "";

export function connectMissionEvents(
  missionId: string,
  onEvent: (event: WsEvent) => void,
  onError?: (error: Event) => void,
): () => void {
  const ws = new WebSocket(`${WS_BASE}/ws/missions/${missionId}/events`);

  ws.onmessage = (msg) => {
    try {
      const parsed = WsEvent.parse(JSON.parse(msg.data));
      onEvent(parsed);
    } catch {
      // ignore malformed frames
    }
  };

  ws.onerror = (err) => {
    onError?.(err);
  };

  return () => {
    ws.close();
  };
}

export function connectGlobalEvents(
  token: string,
  onEvent: (event: WsEvent) => void,
): () => void {
  const ws = new WebSocket(
    `${WS_BASE}/ws/missions/*/events?token=${encodeURIComponent(token)}`,
  );

  ws.onmessage = (msg) => {
    try {
      const parsed = WsEvent.parse(JSON.parse(msg.data));
      onEvent(parsed);
    } catch {
      // ignore malformed frames
    }
  };

  return () => {
    ws.close();
  };
}
