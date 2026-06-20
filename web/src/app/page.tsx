"use client";

import { useEffect, useState } from "react";
import { listMissions } from "@/lib/api";
import type { MissionRead } from "@/lib/types";
import { MissionCard } from "@/components/mission-card";

export default function HomePage() {
  const [missions, setMissions] = useState<MissionRead[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listMissions()
      .then(setMissions)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  return (
    <div>
      <h1 className="mb-6 text-2xl font-bold">Missions</h1>

      {loading && <p className="text-zinc-400">Loading...</p>}

      {error && (
        <p className="rounded-md border border-red-800 bg-red-950 p-3 text-sm text-red-200">
          {error}
        </p>
      )}

      {!loading && !error && missions.length === 0 && (
        <div className="rounded-lg border border-zinc-800 bg-zinc-900 p-8 text-center">
          <p className="text-zinc-400">No missions yet.</p>
          <a
            href="/missions/new"
            className="mt-3 inline-block text-sm text-zinc-200 underline underline-offset-2 hover:text-white"
          >
            Create your first mission
          </a>
        </div>
      )}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {missions.map((m) => (
          <MissionCard key={m.id} mission={m} />
        ))}
      </div>
    </div>
  );
}
