import { Card } from "./ui/card";

interface Props {
  gitInfo: Record<string, unknown> | null;
  branch: string | null | undefined;
}

export function GitInfoPanel({ gitInfo, branch }: Props) {
  if (!gitInfo && !branch) {
    return (
      <Card>
        <p className="text-sm text-zinc-500">
          No git info available. Create a branch via the action bar above.
        </p>
      </Card>
    );
  }

  return (
    <Card title="Git Info">
      <dl className="space-y-3 text-sm">
        {branch && (
          <div>
            <dt className="text-zinc-500">Branch</dt>
            <dd className="font-mono text-xs text-zinc-200">{branch}</dd>
          </div>
        )}
        {gitInfo && String(gitInfo["commit_sha"] ?? "") && (
          <div>
            <dt className="text-zinc-500">Commit SHA</dt>
            <dd className="font-mono text-xs text-zinc-200">
              {String(gitInfo["commit_sha"])}
            </dd>
          </div>
        )}
        {gitInfo && String(gitInfo["merge_sha"] ?? "") && (
          <div>
            <dt className="text-zinc-500">Merge SHA</dt>
            <dd className="font-mono text-xs text-green-300">
              {String(gitInfo["merge_sha"])}
            </dd>
          </div>
        )}
      </dl>
    </Card>
  );
}
