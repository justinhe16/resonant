import { Empty, Group, Row, Rows, Section, StateBadge } from "@/components/bits";
import { Badge } from "@/components/ui/badge";
import type { Repo, TrustRule } from "@/types";

/** Local tier only. */
export function Rules({ rules, repos }: { rules: TrustRule[]; repos: Repo[] }) {
  return (
    <Section title="Rules and repos" description="Change rules with /rules on iMessage">
      <div className="space-y-5">
        <Group label="Trust rules" count={rules.length}>
          {rules.length === 0 ? (
            <Empty>No rules yet: workers ask before anything net new.</Empty>
          ) : (
            <ol className="divide-y">
              {rules.map((r) => (
                <li key={r.n} className="flex gap-3 py-2.5 text-sm first:pt-0 last:pb-0">
                  <span className="w-6 shrink-0 text-right font-mono text-muted-foreground tabular-nums">{r.n}</span>
                  <span>{r.text}</span>
                </li>
              ))}
            </ol>
          )}
        </Group>
        <Group label="Repos" count={repos.length}>
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
                  {r.ci === false && <Badge variant="warn">no CI</Badge>}
                </Row>
              ))}
            </Rows>
          )}
        </Group>
      </div>
    </Section>
  );
}
