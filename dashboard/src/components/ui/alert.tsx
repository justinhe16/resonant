import { cva, type VariantProps } from "class-variance-authority";
import type { HTMLAttributes } from "react";
import { cn } from "@/lib/utils";

const alertVariants = cva("flex items-start gap-3 rounded-lg border px-4 py-3 text-sm", {
  variants: {
    tone: {
      neutral: "bg-card",
      warn: "border-warn/40 bg-warn/10",
      bad: "border-bad/40 bg-bad/10",
    },
  },
  defaultVariants: { tone: "neutral" },
});

export function Alert({
  className,
  tone,
  ...props
}: HTMLAttributes<HTMLDivElement> & VariantProps<typeof alertVariants>) {
  return <div role="alert" className={cn(alertVariants({ tone }), className)} {...props} />;
}
