import { cn } from "@/lib/utils";

const variants: Record<string, string> = {
  default: "bg-zinc-800 text-zinc-100",
  created: "bg-zinc-700 text-zinc-200",
  planning: "bg-blue-900 text-blue-200",
  approved: "bg-emerald-900 text-emerald-200",
  execution: "bg-amber-900 text-amber-200",
  reviewed: "bg-violet-900 text-violet-200",
  merged: "bg-green-900 text-green-200",
  rejected: "bg-red-900 text-red-200",
  escalated: "bg-orange-900 text-orange-200",
};

export function Badge({
  label,
  variant = "default",
  className,
}: {
  label: string;
  variant?: string;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium capitalize",
        variants[variant] ?? variants.default,
        className,
      )}
    >
      {label}
    </span>
  );
}
