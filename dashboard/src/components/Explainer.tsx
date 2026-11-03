import { Info } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { EXPLAINERS, type ExplainerKey } from "@/lib/explainers";

/** The (i) button on a section header: a popover with what the section is, in plain words. */
export function Explainer({ name, text }: { name: string; text: ExplainerKey }) {
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={`About ${name}`}
          className="inline-flex size-6 shrink-0 items-center justify-center rounded-full text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50 [&_svg]:size-3.5"
        >
          <Info />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-96 text-xs leading-relaxed" role="note" aria-label={`About ${name}`}>
        {EXPLAINERS[text]}
      </PopoverContent>
    </Popover>
  );
}
