import { cn } from "@/lib/utils";
import { ButtonHTMLAttributes, forwardRef } from "react";

type Variant = "default" | "secondary" | "destructive" | "ghost";

const variantClasses: Record<Variant, string> = {
  default: "bg-zinc-100 text-zinc-900 hover:bg-zinc-200",
  secondary: "bg-zinc-800 text-zinc-100 hover:bg-zinc-700",
  destructive: "bg-red-600 text-white hover:bg-red-700",
  ghost: "text-zinc-300 hover:bg-zinc-800 hover:text-zinc-100",
};

const sizeClasses: Record<string, string> = {
  sm: "h-8 px-3 text-sm",
  md: "h-9 px-4 text-sm",
  lg: "h-10 px-5 text-base",
};

export const Button = forwardRef<
  HTMLButtonElement,
  ButtonHTMLAttributes<HTMLButtonElement> & {
    variant?: Variant;
    size?: keyof typeof sizeClasses;
  }
>(({ className, variant = "default", size = "md", disabled, ...props }, ref) => (
  <button
    ref={ref}
    disabled={disabled}
    className={cn(
      "inline-flex items-center justify-center rounded-md font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-zinc-400 disabled:pointer-events-none disabled:opacity-50",
      variantClasses[variant],
      sizeClasses[size],
      className,
    )}
    {...props}
  />
));
Button.displayName = "Button";
