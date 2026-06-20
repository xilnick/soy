import { Badge } from "./ui/badge";
import type { MissionRead } from "@/lib/types";

export function MissionCard({ mission }: { mission: MissionRead }) {
  return (
    <a
      href={`/missions/${mission.id}`}
      className="block rounded-lg border border-zinc-800 bg-zinc-900 p-4 transition-colors hover:border-zinc-600"
    >
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-zinc-100 truncate">
          {mission.title}
        </h3>
        <Badge label={mission.status} variant={mission.status} />
      </div>
      {mission.description && (
        <p className="mb-2 line-clamp-2 text-xs text-zinc-500">
          {mission.description}
        </p>
      )}
      <div className="flex gap-3 text-xs text-zinc-600">
        {mission.branch && <span className="font-mono">{mission.branch}</span>}
        {mission.repo_url && (
          <span className="truncate">{mission.repo_url}</span>
        )}
      </div>
    </a>
  );
}
