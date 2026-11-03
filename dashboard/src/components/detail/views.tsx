import type { ReactNode } from "react";
import { Ago, Empty, ExternalLink, Group, Meta, OutsideText, RouteLink, Row, Rows, StateBadge } from "@/components/bits";
import { ApprovalLinks, DiagramText, Missing, NodeLink, RunLines, RunLink, TaskLink, idChip } from "@/components/detail/links";
import { F, Fields, Timeline, When } from "@/components/detail/parts";
import { Badge } from "@/components/ui/badge";
import { DecisionBadge } from "@/sections/Claude";
import { JobRow } from "@/sections/Extensions";
import { Changes } from "@/sections/WaitingOnYou";
import { connections, KIND_LABEL } from "@/lib/diagram";
import type { Route } from "@/lib/route";
import { money, num } from "@/lib/format";
import { approvalTitle, findApproval, githubLinks, isOutside, safeHttpsUrl } from "@/lib/snapshot";
import { span } from "@/lib/time";
import { approvalTone, healthTone, levelTone, runTone, taskTone, urgencyTone } from "@/lib/tone";
import type { Job, Snapshot } from "@/types";

export interface View {
  title: string;
  description?: string;
  body: ReactNode;
}

interface Ctx {
  snap: Snapshot;
  now: number;
}

const notKept = (title: string, what: string, keeps: string, snap: Snapshot): View => ({
  title,
  body: <Missing what={what} keeps={keeps} snap={snap} />,
});

function runView(code: string, { snap, now }: Ctx): View {
  const run = snap.runs.items.find((r) => r.code === code);
  if (!run)
    return notKept(`Run ${code}`, code, `it keeps the newest ${snap.limits.runs} runs`, snap);
  const links = githubLinks(run);
  const remoteUrl = safeHttpsUrl(run.remote_url);
  const qs = run.question_ids.map((id) => snap.questions.items.find((q) => q.id === id) ?? null);
  return {
    title: `Run ${run.code}`,
    description: run.repo || "scratch folder (no repo)",
    body: (
      <div className="space-y-5">
        <div className="flex flex-wrap items-center gap-1.5">
          <StateBadge tone={runTone(run.state)}>
            {run.state}
            {run.waiting_reason ? ` (${run.waiting_reason.replace(/_/g, " ")})` : ""}
          </StateBadge>
          {run.linear_ids.map((l) => (
            <span key={l} className={idChip} title="Linear issue">
              {l}
            </span>
          ))}
        </div>
        {run.ask && (
          <Group label="Ask">
            <p className="text-sm">
              <OutsideText text={run.ask} outside={isOutside(run, "ask")} />
            </p>
          </Group>
        )}
        {run.summary && (
          <Group label="Summary">
            <p className="text-sm">
              <OutsideText text={run.summary} outside={isOutside(run, "summary")} />
            </p>
          </Group>
        )}
        {run.failure && (
          <Group label="Why it failed">
            <p className="text-sm text-bad">
              <OutsideText text={run.failure} outside={isOutside(run, "failure")} />
            </p>
          </Group>
        )}
        {run.last_note && (
          <Group label="Last note">
            <p className="text-sm italic">
              <OutsideText text={run.last_note} outside={isOutside(run, "last_note")} />
            </p>
          </Group>
        )}
        {!run.detail && (
          <p className="text-xs text-muted-foreground" data-testid="no-detail">
            Its summary, history and lists are left out: the snapshot keeps full detail for the newest{" "}
            {snap.limits.run_detail} runs.
          </p>
        )}
        <Fields>
          <F label="Repo">{run.repo || "none (scratch folder)"}</F>
          <F label="Branch">
            {run.branch && (
              <span className="font-mono">
                {run.branch}
                {run.base_branch && <span className="text-muted-foreground"> from {run.base_branch}</span>}
              </span>
            )}
          </F>
          <F label="Commit">{run.head_sha && <span className="font-mono">{run.head_sha.slice(0, 10)}</span>}</F>
          <F label="GitHub">
            {(links.pr || links.branch || links.commit) && (
              <span className="flex flex-wrap gap-x-3 gap-y-1">
                {links.pr && <ExternalLink href={links.pr}>PR #{run.pr_number ?? "?"}</ExternalLink>}
                {links.branch && <ExternalLink href={links.branch}>Branch</ExternalLink>}
                {links.commit && <ExternalLink href={links.commit}>Commit</ExternalLink>}
              </span>
            )}
          </F>
          <F label="Remote Control">{remoteUrl && <ExternalLink href={remoteUrl}>Open session</ExternalLink>}</F>
          <F label="Created">
            <When iso={run.created_at} now={now} />
          </F>
          <F label="Started">
            <When iso={run.started_at} now={now} />
          </F>
          <F label="Ended">
            <When iso={run.ended_at} now={now} />
          </F>
          <F label="Task">
            <TaskLink id={run.task_id} />
          </F>
          <F label="Step">{run.step}</F>
          <F label="Sessions">{run.sessions > 0 && run.sessions}</F>
          <F label="Attempts">{run.attempts > 0 && run.attempts}</F>
          <F label="Session time">{run.session_seconds > 0 && span(run.session_seconds)}</F>
          <F label="Waited">{run.wait_seconds > 0 && span(run.wait_seconds)}</F>
          <F label="Cost">{run.cost_usd > 0 && money(run.cost_usd)}</F>
          <F label="Tokens">
            {(run.tokens_in > 0 || run.tokens_out > 0) && `${num(run.tokens_in)} in, ${num(run.tokens_out)} out`}
          </F>
          <F label="Files changed">{run.files_changed > 0 && run.files_changed}</F>
          <F label="Exit code">{run.exit_code !== null && run.exit_code}</F>
          <F label="Merged">
            {run.merged.length > 0 && (
              <span>
                {run.merged.map((m, i) => (
                  <span key={m}>
                    {i > 0 && ", "}
                    <span className="font-mono">{m}</span>
                  </span>
                ))}
                {run.merged_by && <span className="text-muted-foreground"> by {run.merged_by}</span>}
              </span>
            )}
          </F>
          <F label="Approvals">
            <ApprovalLinks ids={run.approval_ids} snap={snap} />
          </F>
          <F label="Questions">
            {run.question_ids.length > 0 && (
              <span className="flex flex-wrap gap-1.5">
                {run.question_ids.map((id, i) =>
                  qs[i] ? (
                    <RouteLink key={id} to={{ kind: "question", id }} className={`${idChip} text-info`}>
                      #{id}
                    </RouteLink>
                  ) : (
                    <span key={id} className={idChip} title="Not in this snapshot">
                      #{id}
                    </span>
                  ),
                )}
              </span>
            )}
          </F>
        </Fields>
        {run.flagged_paths.length > 0 && (
          <Group label="Flagged paths" count={run.flagged_paths.length}>
            <ul className="space-y-1 font-mono text-xs">
              {run.flagged_paths.map((p) => (
                <li key={p}>
                  <OutsideText text={p} outside={isOutside(run, "flagged_paths")} />
                </li>
              ))}
            </ul>
          </Group>
        )}
        {run.did.length > 0 && (
          <Group label="What it said it did" count={run.did.length}>
            <ul className="list-disc space-y-1 pl-5 text-sm">
              {run.did.map((d, i) => (
                <li key={i}>
                  <OutsideText text={d} outside={isOutside(run, "did")} />
                </li>
              ))}
            </ul>
          </Group>
        )}
        {run.effects.length > 0 && (
          <Group label="Side effects detected in its commands" count={run.effects.length}>
            <ul className="space-y-1.5 text-sm">
              {run.effects.map((e, i) => (
                <li key={i} className="flex flex-wrap items-start gap-x-2 gap-y-1">
                  <RouteLink to={{ kind: "effects", effect: e.kind }}>
                    <Badge variant="neutral">{e.kind}</Badge>
                  </RouteLink>
                  {e.high && <Badge variant="bad">high impact</Badge>}
                  <span className="min-w-0 font-mono text-xs break-all">
                    <OutsideText text={e.what} outside={isOutside(run, "effects")} />
                  </span>
                </li>
              ))}
            </ul>
          </Group>
        )}
        <Group label="History">
          <Timeline
            events={run.history}
            now={now}
            empty={run.detail ? "No events recorded." : "Not kept for this run."}
          />
        </Group>
      </div>
    ),
  };
}

function taskView(id: string, { snap, now }: Ctx): View {
  const t = snap.tasks.items.find((x) => x.id === id);
  if (!t)
    return notKept(`Task ${id}`, id, `it keeps ${snap.limits.tasks} tasks: the active ones, then the newest`, snap);
  return {
    title: t.label ?? `Task ${t.id}`,
    description: `Task ${t.id}`,
    body: (
      <div className="space-y-5">
        <StateBadge tone={taskTone(t.status)}>
          {t.status}
          {t.wait_reason ? ` (${t.wait_reason.replace(/_/g, " ")})` : ""}
        </StateBadge>
        {t.error && (
          <Group label="Error">
            <p className="text-sm text-bad">
              <OutsideText text={t.error} outside={isOutside(t, "error")} />
            </p>
          </Group>
        )}
        <Fields>
          <F label="Kind">{t.kind}</F>
          <F label="Runner">{t.runner}</F>
          <F label="Extension">
            {t.extension && (
              <RouteLink to={{ kind: "extension", name: t.extension }} className="font-mono font-medium text-info">
                {t.extension}
              </RouteLink>
            )}
          </F>
          <F label="Run">
            <RunLink code={t.run_code} />
          </F>
          <F label="Created">
            <When iso={t.created_at} now={now} />
          </F>
          <F label="Updated">
            <When iso={t.updated_at} now={now} />
          </F>
          <F label="Wakes">
            <When iso={t.wake_at} now={now} />
          </F>
          <F label="Attempts">{t.attempts}</F>
          <F label="Step">{t.step_no}</F>
          <F label="Approvals">
            <ApprovalLinks ids={t.approval_ids} snap={snap} />
          </F>
        </Fields>
        <Group label="History">
          <Timeline
            events={t.history}
            now={now}
            empty={`Only the first ${snap.limits.task_detail} tasks carry history.`}
          />
        </Group>
      </div>
    ),
  };
}

function approvalView(id: string, { snap, now }: Ctx): View {
  const a = findApproval(snap, id);
  if (!a)
    return notKept(
      "Approval",
      `Approval ${id}`,
      `it keeps the pending approvals and the newest ${snap.limits.approvals_recent} decided in the last 7 days`,
      snap,
    );
  return {
    title: "Approval",
    description: `${a.tool}${a.extension ? ` · ${a.extension}` : ""}`,
    body: (
      <div className="space-y-5">
        <div className="flex flex-wrap items-center gap-1.5">
          <StateBadge tone={approvalTone(a.status)}>{a.status}</StateBadge>
          <Badge variant={levelTone(a.level)}>{a.level}</Badge>
          <Badge variant="neutral">{a.effect}</Badge>
        </div>
        <p className="text-sm">
          <OutsideText text={approvalTitle(a)} outside={isOutside(a, "summary")} />
        </p>
        <Changes approval={a} />
        <Fields>
          <F label="Needs its code">{a.needs_code ? "yes (yes <code>)" : "no"}</F>
          <F label="Code">{a.code && <span className="font-mono">{a.code}</span>}</F>
          <F label="Asked">
            <When iso={a.asked_at} now={now} />
          </F>
          <F label="Expires">
            <When iso={a.expires_at} now={now} />
          </F>
          <F label="Decided">
            <When iso={a.decided_at} now={now} />
          </F>
          <F label="Decided by">{a.decided_by}</F>
          <F label="Task">
            <TaskLink id={a.task_id} />
          </F>
          <F label="Run">
            <RunLink code={a.run_code} />
          </F>
          <F label="Members">
            {a.member_ids.length > 1 && (
              <span className="flex flex-wrap gap-1.5">
                {a.member_ids.map((m) => (
                  <span key={m} className={idChip}>
                    {m}
                  </span>
                ))}
              </span>
            )}
          </F>
        </Fields>
      </div>
    ),
  };
}

function questionView(id: number, { snap, now }: Ctx): View {
  const q = snap.questions.items.find((x) => x.id === id);
  if (!q) return notKept(`Question #${id}`, `Question #${id}`, `it keeps the ${snap.limits.questions} open ones`, snap);
  return {
    title: `Question from ${q.run_code}`,
    description: q.kind || undefined,
    body: (
      <div className="space-y-5">
        {q.text && (
          <p className="text-sm">
            <OutsideText text={q.text} outside={isOutside(q, "text")} />
          </p>
        )}
        {q.options.length > 0 && (
          <Group label="Options">
            <ol className="space-y-1 text-sm">
              {q.options.map((o, i) => (
                <li key={i}>
                  ({String.fromCharCode(97 + i)}) <OutsideText text={o} outside={isOutside(q, "options")} />
                </li>
              ))}
            </ol>
          </Group>
        )}
        <Fields>
          <F label="Run">
            <RunLink code={q.run_code} />
          </F>
          <F label="Urgent">{q.urgent && "yes"}</F>
          <F label="Asked">
            <When iso={q.asked_at} now={now} />
          </F>
        </Fields>
      </div>
    ),
  };
}

function extensionView(name: string, { snap, now }: Ctx): View {
  const e = snap.extensions.find((x) => x.name === name);
  if (!e) return notKept(`Extension ${name}`, `Extension ${name}`, "it lists the installed extensions", snap);
  const muted = !!e.muted_until && Date.parse(e.muted_until) > now;
  return {
    title: e.name,
    description: e.description ?? undefined,
    body: (
      <div className="space-y-5">
        <DiagramText snap={snap} id={`ext:${e.name}`} />
        <div className="flex flex-wrap items-center gap-1.5">
          <StateBadge tone={healthTone(e.health)}>{e.health}</StateBadge>
          <Badge variant={e.status === "enabled" ? "ok" : "warn"}>{e.status.replace(/_/g, " ")}</Badge>
          {muted && <Badge variant="warn">muted</Badge>}
        </div>
        <Fields>
          <F label="Source">{e.source_kind}</F>
          <F label="Commit">{e.commit && <span className="font-mono">{e.commit}</span>}</F>
          <F label="Installed">
            <When iso={e.installed_at || null} now={now} />
          </F>
          <F label="Muted until">{muted && <When iso={e.muted_until} now={now} />}</F>
          <F label="Reports up to">{e.report_max}</F>
          <F label="Secrets">
            {e.secrets.length > 0 && (
              <span className="flex flex-wrap gap-1.5">
                {e.secrets.map((s) => (
                  <span key={s} className={idChip}>
                    {s}
                  </span>
                ))}
              </span>
            )}
          </F>
          <F label="Repos">
            {e.repos.length > 0 && (
              <span className="flex flex-wrap gap-1.5">
                {e.repos.map((r) => (
                  <span key={r} className={idChip}>
                    {r}
                  </span>
                ))}
              </span>
            )}
          </F>
        </Fields>
        {e.mcp_servers.length > 0 && (
          <Group label="MCP servers" count={e.mcp_servers.length}>
            <ul className="space-y-1 text-sm">
              {e.mcp_servers.map((m) => (
                <li key={m.id}>
                  <span className="font-mono">{m.id}</span>{" "}
                  <span className="text-muted-foreground">
                    {m.transport}
                    {m.host ? ` · ${m.host}` : ""}
                  </span>
                </li>
              ))}
            </ul>
          </Group>
        )}
        <Group label="Tools" count={e.tools.length}>
          {e.tools.length === 0 ? (
            <Empty>No tools listed.</Empty>
          ) : (
            <Rows>
              {e.tools.map((t) => (
                <Row key={`${t.via}-${t.server ?? ""}-${t.name}`}>
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                    <span className="font-mono text-sm break-all">{t.name}</span>
                    <Badge variant="neutral">{t.via === "mcp" && t.server ? `mcp · ${t.server}` : t.via}</Badge>
                    <Badge variant="neutral">{t.effect}</Badge>
                    <Badge variant={levelTone(t.level)}>{t.level}</Badge>
                  </div>
                  {t.description && <p className="mt-1 text-xs text-muted-foreground">{t.description}</p>}
                </Row>
              ))}
            </Rows>
          )}
        </Group>
        <Group label="Jobs" count={e.jobs.length}>
          {e.jobs.length === 0 ? (
            <Empty>No scheduled jobs.</Empty>
          ) : (
            <Rows>
              {e.jobs.map((j) => (
                <JobRow key={j.id} job={j} ext={e.name} now={now} />
              ))}
            </Rows>
          )}
        </Group>
      </div>
    ),
  };
}

function jobView(ext: string, id: string, { snap, now }: Ctx): View {
  const owner = ext === "core" ? null : snap.extensions.find((e) => e.name === ext);
  const jobs: Job[] = ext === "core" ? snap.core_jobs : (owner?.jobs ?? []);
  const j = jobs.find((x) => x.id === id);
  if (!j) return notKept(`Job ${ext}/${id}`, `Job ${ext}/${id}`, "it lists the scheduled jobs", snap);
  return {
    title: `${ext}/${j.id}`,
    description: ext === "core" ? "A core job" : "An extension job",
    body: (
      <div className="space-y-5">
        <DiagramText snap={snap} id={`job:${ext}/${j.id}`} />
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge variant={j.last_outcome === "ok" ? "ok" : j.last_outcome ? "bad" : "neutral"}>
            {j.last_outcome ?? "not run"}
          </Badge>
          {j.fail_streak > 0 && <Badge variant="bad">{j.fail_streak} in a row</Badge>}
        </div>
        <Fields>
          <F label="Extension">
            {owner && (
              <RouteLink to={{ kind: "extension", name: owner.name }} className="font-mono font-medium text-info">
                {owner.name}
              </RouteLink>
            )}
          </F>
          <F label="Schedule">{j.schedule && <span className="font-mono">{j.schedule}</span>}</F>
          <F label="Time zone">{j.tz}</F>
          <F label="Effect">{j.effect}</F>
          <F label="Level">{j.level}</F>
          <F label="Last run">
            <When iso={j.last_run_at} now={now} />
          </F>
          <F label="Next due">
            <When iso={j.next_due} now={now} />
          </F>
        </Fields>
      </div>
    ),
  };
}

function pulseView(source: string, key: string, { snap, now }: Ctx): View {
  const p = snap.pulse.items.find((x) => x.source === source && x.key === key);
  if (!p)
    return notKept(`${source}/${key}`, `${source}/${key}`, `it keeps ${snap.limits.pulse} open pulse items`, snap);
  return {
    title: `${p.source}/${p.key}`,
    description: "Pulse item",
    body: (
      <div className="space-y-5">
        <div className="flex flex-wrap items-center gap-1.5">
          <StateBadge tone={urgencyTone(p.urgency)}>{p.urgency}</StateBadge>
          <Badge variant={p.state === "new" ? "warn" : "neutral"}>{p.state === "new" ? "new: not told yet" : "told"}</Badge>
        </div>
        <p className="text-sm">
          <OutsideText text={p.title || p.source} outside={isOutside(p, "title")} />
        </p>
        <Fields>
          <F label="Source">
            {p.source === "core" ? (
              "core"
            ) : (
              <RouteLink to={{ kind: "extension", name: p.source }} className="font-mono font-medium text-info">
                {p.source}
              </RouteLink>
            )}
          </F>
          <F label="Run">
            <RunLink code={p.run_code} />
          </F>
          <F label="Updated">
            <When iso={p.updated_at} now={now} />
          </F>
          <F label="Told">
            <When iso={p.told_at} now={now} />
          </F>
          <F label="Expires">
            <When iso={p.expires_at} now={now} />
          </F>
        </Fields>
      </div>
    ),
  };
}

function healthView(name: string, { snap, now }: Ctx): View {
  const h = snap.health.find((x) => x.name === name);
  if (!h) return notKept(`Check ${name}`, `Check ${name}`, "it lists the health checks", snap);
  const ext = name.startsWith("ext:") ? name.slice(4) : null;
  return {
    title: h.name,
    description: "Health check",
    body: (
      <div className="space-y-5">
        <StateBadge tone={healthTone(h.state)}>{h.state}</StateBadge>
        {h.reason && (
          <p className="text-sm">
            <OutsideText text={h.reason} outside={isOutside(h, "reason")} />
          </p>
        )}
        <Fields>
          <F label="Since">
            <When iso={h.since} now={now} />
          </F>
          <F label="Extension">
            {ext && snap.extensions.some((e) => e.name === ext) && (
              <RouteLink to={{ kind: "extension", name: ext }} className="font-mono font-medium text-info">
                {ext}
              </RouteLink>
            )}
          </F>
        </Fields>
      </div>
    ),
  };
}

function brainToolView(at: string, { snap, now }: Ctx): View {
  const t = snap.claude_brain?.tools.find((x) => x.at === at);
  if (!t)
    return notKept(
      "Tool call",
      "That tool call",
      `it keeps the Claude brain's last ${snap.limits.brain_tools} calls`,
      snap,
    );
  return {
    title: `${t.tool} call`,
    description: "Claude brain",
    body: (
      <div className="space-y-5">
        <div className="flex flex-wrap items-center gap-1.5">
          <DecisionBadge decision={t.decision} status={t.status} />
          {t.write && <Badge variant="warn">change</Badge>}
        </div>
        {t.command && (
          <Group label="Command">
            <p className="font-mono text-sm break-all">
              <OutsideText text={t.command} outside={isOutside(t, "command")} />
            </p>
          </Group>
        )}
        <Fields>
          <F label="Directory">{t.cwd && <span className="font-mono break-all">{t.cwd}</span>}</F>
          <F label="At">
            <When iso={t.at} now={now} />
          </F>
        </Fields>
      </div>
    ),
  };
}

function runsView(state: string | undefined, { snap, now }: Ctx): View {
  const items = state ? snap.runs.items.filter((r) => r.state === state) : snap.runs.items;
  const total = state ? (snap.runs.by_state[state] ?? 0) : Object.values(snap.runs.by_state).reduce((a, b) => a + b, 0);
  return {
    title: state ? `Runs: ${state.replace(/_/g, " ")}` : "Runs",
    description: `${items.length} of ${total} in this snapshot`,
    body:
      items.length === 0 ? (
        <Missing what={state ? `No ${state} run` : "No run"} keeps={`it keeps the newest ${snap.limits.runs} runs`} snap={snap} />
      ) : (
        <div className="space-y-3">
          <RunLines runs={items} now={now} />
          {items.length < total && (
            <p className="text-xs text-muted-foreground">
              The snapshot keeps the newest {snap.limits.runs} runs; older ones aren't listed.
            </p>
          )}
        </div>
      ),
  };
}

function tasksView(state: string | undefined, { snap, now }: Ctx): View {
  const items = state ? snap.tasks.items.filter((t) => t.status === state) : snap.tasks.items;
  const total = state ? (snap.tasks.by_state[state] ?? 0) : Object.values(snap.tasks.by_state).reduce((a, b) => a + b, 0);
  return {
    title: state ? `Tasks: ${state}` : "Tasks",
    description: `${items.length} of ${total} in this snapshot`,
    body:
      items.length === 0 ? (
        <Missing
          what={state ? `No ${state} task` : "No task"}
          keeps={`it keeps ${snap.limits.tasks} tasks: the active ones, then the newest`}
          snap={snap}
        />
      ) : (
        <Rows>
          {items.map((t) => (
            <Row key={t.id} className="relative">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <RouteLink to={{ kind: "task", id: t.id }} stretch className="font-mono text-sm font-semibold">
                    {t.id}
                  </RouteLink>
                  <span className="text-xs text-muted-foreground"> · {t.kind}</span>
                  {t.label && <p className="line-clamp-2 text-sm">{t.label}</p>}
                </div>
                <StateBadge tone={taskTone(t.status)}>{t.status}</StateBadge>
              </div>
              <Meta>
                <Ago iso={t.updated_at} now={now} prefix="updated " />
              </Meta>
            </Row>
          ))}
        </Rows>
      ),
  };
}

function effectsView(kind: string, { snap }: Ctx): View {
  const runs = snap.runs.items.filter((r) => r.effects.some((e) => e.kind === kind));
  const count = snap.runs.detected.by_kind[kind] ?? 0;
  return {
    title: `Side effects: ${kind}`,
    description: `${count} detected in recent runs`,
    body:
      runs.length === 0 ? (
        <Missing
          what={`No run with a detected ${kind} effect`}
          keeps={`it keeps run detail for the newest ${snap.limits.run_detail} runs`}
          snap={snap}
        />
      ) : (
        <div className="space-y-4">
        <ul className="space-y-4">
          {runs.map((r) => (
            <li key={r.code} className="space-y-1.5">
              <RunLink code={r.code} />
              <ul className="space-y-1">
                {r.effects
                  .filter((e) => e.kind === kind)
                  .map((e, i) => (
                    <li key={i} className="flex flex-wrap items-start gap-x-2 text-sm">
                      {e.high && <Badge variant="bad">high impact</Badge>}
                      <span className="min-w-0 font-mono text-xs break-all">
                        <OutsideText text={e.what} outside={isOutside(r, "effects")} />
                      </span>
                    </li>
                  ))}
              </ul>
            </li>
          ))}
        </ul>
          <p className="text-xs text-muted-foreground">Newest runs first.</p>
        </div>
      ),
  };
}

function nodeView(id: string, { snap, now }: Ctx): View {
  const d = snap.diagram;
  const n = d?.nodes.find((x) => x.id === id);
  if (!d || !n) return notKept("Diagram", `Component ${id}`, "it lists what the daemon knows about right now", snap);
  const { into, out } = connections(d, n.id);
  const parent = n.parent ? d.nodes.find((x) => x.id === n.parent) : undefined;
  const ext = n.ext && n.ext !== "core" ? snap.extensions.find((e) => e.name === n.ext) : undefined;
  const check = n.check ? snap.health.find((h) => h.name === n.check) : undefined;
  return {
    title: n.label,
    description: `${KIND_LABEL[n.kind].charAt(0).toUpperCase()}${KIND_LABEL[n.kind].slice(1)} in the diagram`,
    body: (
      <div className="space-y-5">
        {n.text && <p className="text-sm">{n.text}</p>}
        <div className="flex flex-wrap items-center gap-1.5">
          {n.health && <StateBadge tone={healthTone(n.health)}>{n.health}</StateBadge>}
          {n.state && <Badge variant="neutral">{n.state.replace(/_/g, " ")}</Badge>}
          {(n.tags ?? []).map((t) => (
            <Badge key={t} variant="neutral">
              {t}
            </Badge>
          ))}
        </div>
        <Fields>
          <F label="Kind">{KIND_LABEL[n.kind]}</F>
          <F label="Part of">{parent && <NodeLink n={parent} />}</F>
          <F label="Extension">
            {ext && (
              <RouteLink to={{ kind: "extension", name: ext.name }} className="font-mono font-medium text-info">
                {ext.name}
              </RouteLink>
            )}
          </F>
          <F label="Code">{n.module && <span className="font-mono">{n.module}</span>}</F>
          <F label="Health check">
            {check && (
              <RouteLink to={{ kind: "health", name: check.name }} className="font-mono font-medium text-info">
                {check.name}
              </RouteLink>
            )}
          </F>
          <F label="Checked since">{check && <When iso={check.since} now={now} />}</F>
          <F label="Last run">{n.last_run_at && <When iso={n.last_run_at} now={now} />}</F>
          <F label="Last outcome">{n.last_outcome}</F>
        </Fields>
        {check?.reason && (
          <p className="text-sm text-muted-foreground">
            <OutsideText text={check.reason} outside={isOutside(check, "reason")} />
          </p>
        )}
        {(into.length > 0 || out.length > 0) && (
          <Group label="Talks to" count={into.length + out.length}>
            <ul className="space-y-1 text-sm">
              {into.map(([e, other]) => (
                <li key={`in-${other.id}`}>
                  <span className="text-muted-foreground">from </span>
                  <NodeLink n={other} />
                  {e.label && <span className="text-muted-foreground"> ({e.label})</span>}
                </li>
              ))}
              {out.map(([e, other]) => (
                <li key={`out-${other.id}`}>
                  <span className="text-muted-foreground">to </span>
                  <NodeLink n={other} />
                  {e.label && <span className="text-muted-foreground"> ({e.label})</span>}
                </li>
              ))}
            </ul>
          </Group>
        )}
      </div>
    ),
  };
}

/** What the dialog shows for a route in this snapshot. */
export function describeRoute(route: Route, ctx: Ctx): View {
  switch (route.kind) {
    case "run":
      return runView(route.code, ctx);
    case "runs":
      return runsView(route.state, ctx);
    case "task":
      return taskView(route.id, ctx);
    case "tasks":
      return tasksView(route.state, ctx);
    case "effects":
      return effectsView(route.effect, ctx);
    case "approval":
      return approvalView(route.id, ctx);
    case "question":
      return questionView(route.id, ctx);
    case "extension":
      return extensionView(route.name, ctx);
    case "job":
      return jobView(route.ext, route.id, ctx);
    case "pulse":
      return pulseView(route.source, route.key, ctx);
    case "health":
      return healthView(route.name, ctx);
    case "brain-tool":
      return brainToolView(route.at, ctx);
    case "node":
      return nodeView(route.id, ctx);
  }
}
