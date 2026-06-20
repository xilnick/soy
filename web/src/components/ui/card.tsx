import { cn } from "@/lib/utils";
import { ReactNode } from "react";

export function Card({
  title,
  children,
  className,
}: {
  title?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "rounded-lg border border-zinc-800 bg-zinc-900 p-4",
        className,
      )}
    >
      {title && (
        <h3 className="mb-3 text-sm font-semibold text-zinc-200">{title}</h3>
      )}
      {children}
    </div>
  );
}
