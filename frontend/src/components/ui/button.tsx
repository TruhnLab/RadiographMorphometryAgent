import { cn } from "../../lib/utils";

type Props = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "ghost" | "outline";
};

export function Button({ className, variant = "primary", ...props }: Props) {
  const base =
    "inline-flex items-center justify-center gap-2 rounded-lg text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed focus:outline-none focus:ring-2 focus:ring-sky-400/40";
  const variants = {
    primary: "bg-sky-600 text-white hover:bg-sky-700 px-4 py-2",
    outline: "border border-slate-300 text-slate-700 hover:bg-slate-100 px-4 py-2",
    ghost: "text-slate-600 hover:bg-slate-100 px-3 py-1.5",
  } as const;
  return <button className={cn(base, variants[variant], className)} {...props} />;
}
