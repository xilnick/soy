"use client";

import { useEffect, useRef, useState } from "react";
import { connectMissionEvents } from "@/lib/ws";
import type { WsEvent } from "@/lib/types";

export function LiveEventStream({ missionId }: { missionId: string }) {
  const [events, setEvents] = useState<WsEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const disconnect = connectMissionEvents(
      missionId,
      (event) => {
        setConnected(true);
        setEvents((prev) => {
          const next = [...prev, event];
          return next.length > 200 ? next.slice(-200) : next;
        });
      },
      () => setConnected(false),
    );
    return disconnect;
  }, [missionId]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [events.length]);

  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900">
      <div className="flex items-center justify-between border-b border-zinc-800 px-4 py-2">
        <h3 className="text-sm font-semibold text-zinc-200">Live Events</h3>
        <span className="flex items-center gap-1.5 text-xs">
          <span
            className={`inline-block h-2 w-2 rounded-full ${
              connected ? "bg-green-400" : "bg-zinc-600"
            }`}
          />
          {connected ? "Connected" : "Disconnected"}
        </span>
      </div>

      <div className="h-96 overflow-auto p-4">
        {events.length === 0 && (
          <p className="text-sm text-zinc-500">
            Waiting for events... Actions performed on this mission will appear
            here in real time.
          </p>
        )}

        {events.map((ev, i) => (
          <div
            key={i}
            className="mb-2 rounded border border-zinc-800 bg-zinc-950 px-3 py-2"
          >
            <div className="mb-1 flex items-center gap-2">
              <span className="rounded bg-zinc-800 px-1.5 py-0.5 text-xs font-mono text-zinc-300">
                {ev.type}
              </span>
              <span className="text-xs text-zinc-600">
                {new Date(ev.timestamp).toLocaleTimeString()}
              </span>
            </div>
            <pre className="whitespace-pre-wrap break-all text-xs text-zinc-500">
              {JSON.stringify(ev.payload, null, 2)}
            </pre>
          </div>
        ))}

        <div ref={endRef} />
      </div>
    </div>
  );
}
