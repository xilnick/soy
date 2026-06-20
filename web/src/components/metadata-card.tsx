import { Card } from "./ui/card";
import { Badge } from "./ui/badge";

interface Props {
  title: string;
  items: Record<string, unknown>[];
  emptyLabel?: string;
}

export function MetadataCard({ title, items, emptyLabel }: Props) {
  if (items.length === 0) {
    return (
      <Card>
        <p className="text-sm text-zinc-500">
          {emptyLabel ?? "No data yet."}
        </p>
      </Card>
    );
  }

  return (
    <Card title={title}>
      <div className="space-y-3">
        {items.map((item, i) => (
          <div
            key={i}
            className="rounded-md border border-zinc-800 bg-zinc-950 p-3"
          >
            <div className="mb-2 flex items-center gap-2">
              {String(item.status ?? "") && (
                <Badge label={String(item.status)} />
              )}
              {String(item.verdict ?? "") && (
                <Badge
                  label={String(item.verdict)}
                  variant={
                    item.verdict === "pass"
                      ? "merged"
                      : item.verdict === "fail"
                        ? "rejected"
                        : "default"
                  }
                />
              )}
              {String(item.agent ?? "") && (
                <span className="text-xs text-zinc-500">
                  via {String(item.agent)}
                </span>
              )}
              {String(item.model ?? "") && (
                <span className="font-mono text-xs text-zinc-600">
                  {String(item.model)}
                </span>
              )}
            </div>

            {String(item.query ?? "") && (
              <p className="mb-2 text-xs text-zinc-400">
                Query: {String(item.query)}
              </p>
            )}

            {String(item.prompt ?? "") && (
              <p className="mb-2 text-xs text-zinc-400">
                Prompt: {String(item.prompt)}
              </p>
            )}

            {String(item.output ?? "") && (
              <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded bg-zinc-900 p-2 text-xs text-zinc-300">
                {String(item.output)}
              </pre>
            )}

            {String(item.error ?? "") && (
              <p className="mt-2 text-xs text-red-300">
                Error: {String(item.error)}
              </p>
            )}

            {String(item.started_at ?? "") && (
              <p className="mt-2 text-xs text-zinc-600">
                {new Date(String(item.started_at)).toLocaleString()}
                {item.duration_seconds != null &&
                  ` (${Number(item.duration_seconds).toFixed(1)}s)`}
              </p>
            )}
          </div>
        ))}
      </div>
    </Card>
  );
}
