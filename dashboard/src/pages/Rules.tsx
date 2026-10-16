import { Empty, Row, Rows, Section, StateBadge } from "@/components/bits";
import { Badge } from "@/components/ui/badge";
import type { Repo, TrustRule } from "@/types";

export function Rules({ rules, repos }: { rules: TrustRule[]; repos: Repo[] }) {
  return (
    <div className="space-y-4">
      <Section title="Trust rules" description="Change them with /rules on iMessage">
        {rules.length === 0 ? (
          <Empty>No rules yet: workers ask before anything net new.</Empty>
        ) : (
          <ol className="-my-1 divide-y">
            {rules.map((r) => (
              <li key={r.n} className="flex gap-3 py-2.5 text-sm">
                <span className="w-6 shrink-0 text-right font-mono text-muted-foreground tabular-nums">{r.n}</span>
                <span>{r.text}</span>
              </li>
            ))}
          </ol>
        )}
      </Section>
      <Section title="Repos">
        {repos.length === 0 ? (
          <Empty>No repos added.</Empty>
        ) : (
          <Rows>
            {repos.map((r) => (
              <Row key={r.name} className="flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <p className="truncate font-mono text-sm">{r.name}</p>
                  <p className="text-xs text-muted-foreground">
                    {r.mode} · base {r.base}
                  </p>
                </div>
                {r.ci === true && <StateBadge tone="ok">CI</StateBadge>}
                {r.ci === false && <Badge tone="warn">no CI</Badge>}
              </Row>
            ))}
          </Rows>
        )}
      </Section>
    </div>
  );
}
