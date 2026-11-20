"""Registered job tools (PER-298, ``resonant/registered_jobs.py``; ``store/jobs.py``):

- ``add_job{schedule, prompt, repo?, output?, name?}`` (**L2**: asks first, with the code-built
  summary of the schedule in words and the prompt). Claude turns the asker's words into
  five-field cron; ``cron.parse_schedule`` validates it in ``settings.timezone`` and more often
  than every 15 minutes is refused. ``repo`` must be registered. ``output``: ``thread`` (the
  thread it was asked in; the default when there is one), ``dm`` (the default otherwise) or
  ``room:<key>`` (``channels.slack.rooms``). The reply says the schedule in words and the
  next 3 times.
- ``list_jobs`` (L0, grant ``jobs``): every live job with its state, last run and next due.
- ``pause_job{id}``, ``resume_job{id}``, ``delete_job{id}``, ``run_job_now{id}`` (L3: do,
  then report). Resuming never catches up; ``run_job_now`` starts one run now (not while
  the previous one is still going).

They are global builtins, so only the owner and approvers (PER-293) may call them; a member
is refused by the gate (``list_jobs`` too, unless granted ``builtin:jobs``). The job's
creator is the calling task's principal; its runs are theirs. Report lines are the tools'
own code-built ``text`` (``toolcore.reports``).
"""

from __future__ import annotations

from typing import Any, cast

from resonant import registered_jobs as rj
from resonant.executor import CallContext, ContextHandler, ToolFailed, ToolHandler
from resonant.extensions.cron import CronError, parse_schedule
from resonant.gate.codes import clean_external
from resonant.gate.types import Intent
from resonant.store import jobs as store
from resonant.store import repos as repos_store
from resonant.store import runs as runs_store
from resonant.store import tasks as task_store
from resonant.tools.base import NO_ARGS, BuiltinContext, no_args
from resonant_sdk import Effect, Level, ToolSpec

GRANT = "jobs"
SCHEDULE_MAX = 100
OUTPUT_MAX = 40
PROMPT_SHOWN = 80  # list_jobs
PROMPT_ASKED = 1000  # the add_job approval: what runs unattended, shown nearly whole
JOBS_SHOWN = 25


def _id_args() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "id": {"type": "string", "minLength": 1, "maxLength": 12, "description": "like J3"}
        },
        "required": ["id"],
        "additionalProperties": False,
    }


ADD_JOB = ToolSpec(
    name="add_job",
    description=(
        "Register a recurring job: on a cron schedule, start a Claude worker run with "
        "`prompt` ('every Monday at 9 check flaky CI and file tickets'). Turn the words into "
        "five-field cron in local time (minute hour day month weekday, e.g. '0 9 * * 1'); at "
        "most every 15 minutes. `repo` (a registered repo) when the work is in one. `output`: "
        "'thread' (this thread, the default), 'dm', or 'room:<key>' for a configured room. "
        "Asks first. The reply says the schedule in words and the next times."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "schedule": {"type": "string", "minLength": 1, "maxLength": SCHEDULE_MAX},
            "prompt": {"type": "string", "minLength": 1, "maxLength": store.PROMPT_MAX},
            "repo": {"type": "string", "maxLength": 200},
            "output": {"type": "string", "maxLength": OUTPUT_MAX},
            "name": {"type": "string", "minLength": 1, "maxLength": store.NAME_MAX},
        },
        "required": ["schedule", "prompt"],
        "additionalProperties": False,
    },
    effect=Effect.WRITE,
    level=Level.L2,
    timeout_s=10.0,
)
LIST_JOBS = ToolSpec(
    name="list_jobs",
    description=(
        "The registered jobs: id, name, schedule in words, state (active or paused), last run "
        "and next due. Read only."
    ),
    args_schema=NO_ARGS,
    effect=Effect.READ,
    timeout_s=10.0,
    grant=GRANT,
)
PAUSE_JOB = ToolSpec(
    name="pause_job",
    description="Pause a registered job (like J3): it stops firing until resumed.",
    args_schema=_id_args(),
    effect=Effect.WRITE,
    level=Level.L3,
    timeout_s=10.0,
    approval_summary="pause job {id}",
)
RESUME_JOB = ToolSpec(
    name="resume_job",
    description="Resume a paused job (like J3). Times missed while paused are not caught up.",
    args_schema=_id_args(),
    effect=Effect.WRITE,
    level=Level.L3,
    timeout_s=10.0,
    approval_summary="resume job {id}",
)
DELETE_JOB = ToolSpec(
    name="delete_job",
    description="Delete a registered job (like J3) for good. A run already going carries on.",
    args_schema=_id_args(),
    effect=Effect.WRITE,
    level=Level.L3,
    timeout_s=10.0,
    approval_summary="delete job {id}",
)
RUN_JOB_NOW = ToolSpec(
    name="run_job_now",
    description="Start one run of a registered job (like J3) now, outside its schedule.",
    args_schema=_id_args(),
    effect=Effect.WRITE,
    level=Level.L3,
    timeout_s=60.0,
    approval_summary="run job {id} now",
)
SPECS: tuple[ToolSpec, ...] = (ADD_JOB, LIST_JOBS, PAUSE_JOB, RESUME_JOB, DELETE_JOB, RUN_JOB_NOW)
NAMES = frozenset(s.name for s in SPECS)


def _clip(text: str, limit: int) -> str:
    flat = clean_external(text)
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def summary_action(intent: Intent) -> str | None:
    """The approval heading for ``add_job``, code-built from every argument that decides what
    runs: ``add job "flaky CI": every Monday at 09:00, start a run in dori: "check flaky CI
    …", output to this thread``."""
    if intent.extension is not None or intent.tool != ADD_JOB.name:
        return None
    a = intent.args
    schedule, prompt = a.get("schedule"), a.get("prompt")
    if not isinstance(schedule, str) or not isinstance(prompt, str):
        return None
    name = a.get("name")
    head = f'add job "{_clip(name, store.NAME_MAX)}"' if isinstance(name, str) else "add a job"
    repo = a.get("repo")
    where = f" in {_clip(repo, 60)}" if isinstance(repo, str) and repo.strip() else ""
    output = a.get("output")
    out = _clip(output, OUTPUT_MAX) if isinstance(output, str) and output.strip() else "thread"
    when = rj.describe(" ".join(schedule.split()))
    flat = clean_external(prompt)
    more = f" (+{len(flat) - PROMPT_ASKED + 1} more chars)" if len(flat) > PROMPT_ASKED else ""
    return (
        f"{head}: {when} ({_clip(schedule, SCHEDULE_MAX)}), start a run{where}: "
        f'"{_clip(prompt, PROMPT_ASKED)}"{more}, output to {out}'
    )


class _Tools:
    def __init__(self, ctx: BuiltinContext) -> None:
        self.ctx = ctx

    @property
    def tz(self) -> str:
        return self.ctx.state.settings.timezone

    def _caller(self, call: CallContext | None) -> tuple[str, dict[str, Any] | None]:
        """The calling task's principal and ``channel_ref`` (the owner, None without one)."""
        owner = self.ctx.principals.owner
        task = task_store.get_task(self.ctx.conn, call.task_id) if call is not None else None
        if task is None:
            return owner, None
        return task.principal or owner, task.channel_ref

    def _job(self, tool: str, args: dict[str, Any]) -> store.Job:
        if unknown := set(args) - {"id"}:
            raise ToolFailed(f"{tool}: unknown arguments: {sorted(unknown)}")
        jid = store.parse_id(args.get("id"))
        if jid is None:
            raise ToolFailed(f"{tool}: id looks like J3")
        job = store.get(self.ctx.conn, jid)
        if job is None or job.state == "deleted":
            raise ToolFailed(f"{tool}: no job {jid}")
        return job

    def _output(self, raw: object, ref: dict[str, Any] | None) -> str:
        threaded = ref is not None and isinstance(ref.get("thread"), str) and bool(ref["thread"])
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return "thread" if threaded else "dm"
        if not isinstance(raw, str):
            raise ToolFailed("add_job: output is thread, dm or room:<key>")
        out = raw.strip().lower().removeprefix("#")
        if out in ("thread", "here", "this thread"):
            if not threaded:
                raise ToolFailed("add_job: there is no thread here; use dm or room:<key>")
            return "thread"
        if out == "dm":
            return "dm"
        key = out.removeprefix("room:").removeprefix("#")
        rooms = self.ctx.state.settings.channels.slack.rooms
        if key in rooms:
            return f"room:{key}"
        known = ", ".join(sorted(rooms)) or "none are set up"
        raise ToolFailed(f"add_job: output is thread, dm or room:<key> (rooms: {known})")

    def _repo(self, raw: object) -> str | None:
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return None
        if not isinstance(raw, str):
            raise ToolFailed("add_job: repo is a registered repo's name")
        found = repos_store.find(self.ctx.conn, raw.strip())
        if found is None:
            names = ", ".join(r.name for r in repos_store.list_all(self.ctx.conn)[:10])
            raise ToolFailed(
                f"add_job: no registered repo {raw.strip()!r}"
                + (f" (registered: {names})" if names else "")
            )
        return found.name

    def _describe(self, job: store.Job) -> dict[str, Any]:
        now = self.ctx.clock()
        nxt: str | None = None
        if job.state == "active":
            try:
                times = rj.next_times(parse_schedule(job.schedule, job.tz), now, 1)
            except CronError:
                times = []
            nxt = rj.fmt_time(times[0], job.tz) if times else None
        last: dict[str, Any] | None = None
        if job.last_run_code is not None:
            run = runs_store.get(self.ctx.conn, job.last_run_code)
            last = {
                "code": job.last_run_code,
                "status": None if run is None else run.status,
                "at": None if job.last_run_at is None else rj.fmt_time(job.last_run_at, job.tz),
            }
        return {
            "id": job.id,
            "name": job.name,
            "schedule": job.schedule,
            "when": rj.describe(job.schedule),
            "tz": job.tz,
            "state": job.state,
            "repo": job.repo,
            "output": job.output,
            "created_by": job.created_by,
            "prompt": _clip(job.prompt, PROMPT_SHOWN),
            "last_run": last,
            "last_error": None if job.last_error is None else _clip(job.last_error, 120),
            "next_due": nxt,
        }

    def _line(self, d: dict[str, Any]) -> str:
        state = d["state"]
        bits = [f"{d['id']} {d['name']}: {d['when']}", state]
        last = d.get("last_run")
        if isinstance(last, dict):
            status = cast(dict[str, Any], last).get("status") or "?"
            bits.append(f"last {cast(dict[str, Any], last)['code']} {status}")
        else:
            bits.append("no runs yet")
        if d.get("next_due"):
            bits.append(f"next {d['next_due']}")
        if d.get("last_error"):
            bits.append(f"⚠ {d['last_error']}")
        return " · ".join(bits)

    # --- handlers ---------------------------------------------------------------------------

    async def add(self, args: dict[str, Any], call: CallContext | None = None) -> dict[str, Any]:
        if unknown := set(args) - {"schedule", "prompt", "repo", "output", "name"}:
            raise ToolFailed(f"add_job: unknown arguments: {sorted(unknown)}")
        raw_schedule, raw_prompt = args.get("schedule"), args.get("prompt")
        if not isinstance(raw_schedule, str) or not isinstance(raw_prompt, str):
            raise ToolFailed("add_job: schedule and prompt are required strings")
        prompt = raw_prompt.strip()
        if not prompt or len(prompt) > store.PROMPT_MAX:
            raise ToolFailed(f"add_job: prompt is 1 to {store.PROMPT_MAX} characters")
        try:
            schedule = rj.parse(raw_schedule, self.tz)
        except CronError as e:
            raise ToolFailed(f"add_job: {e}") from e
        principal, ref = self._caller(call)
        output = self._output(args.get("output"), ref)
        repo = self._repo(args.get("repo"))
        raw_name = args.get("name")
        if raw_name is not None and not isinstance(raw_name, str):
            raise ToolFailed("add_job: name is a string")
        name = " ".join((raw_name or "").split())[: store.NAME_MAX] or _clip(prompt, 40)
        if store.count_live(self.ctx.conn) >= rj.MAX_JOBS:
            raise ToolFailed(f"add_job: {rj.MAX_JOBS} jobs already; delete one first")
        now = self.ctx.clock()
        job = store.create(
            self.ctx.conn,
            name=name,
            schedule=schedule.expr,
            tz=self.tz,
            prompt=prompt,
            repo=repo,
            output=output,
            created_by=principal,
            channel_ref=ref,
            now=now,
        )
        times = [rj.fmt_time(t, self.tz) for t in rj.next_times(schedule, now, 3)]
        when = rj.describe(schedule.expr)
        text = (
            f"Added {job.id} ({name}): {when} ({schedule.expr}, {self.tz}). "
            f"Next: {', '.join(times)}. Output: {rj.output_text(job)}."
        )
        return {**self._describe(job), "added": True, "next": times, "text": text}

    async def list(self, args: dict[str, Any], call: CallContext | None = None) -> dict[str, Any]:
        del call
        no_args(LIST_JOBS.name, args)
        jobs = [self._describe(j) for j in store.live(self.ctx.conn)]
        shown = jobs[:JOBS_SHOWN]
        text = "\n".join(["Jobs:", *(self._line(d) for d in shown)]) if shown else "No jobs."
        return {"jobs": shown, "count": len(jobs), "truncated": len(jobs) > JOBS_SHOWN,
                "text": text}  # fmt: skip

    async def pause(self, args: dict[str, Any], call: CallContext | None = None) -> dict[str, Any]:
        del call
        job = self._job(PAUSE_JOB.name, args)
        if job.state == "paused":
            return {"id": job.id, "paused": False, "state": "paused",
                    "text": f"{job.id} ({job.name}) is already paused."}  # fmt: skip
        store.set_state(self.ctx.conn, job.id, "paused", self.ctx.clock(), expect=("active",))
        return {"id": job.id, "paused": True, "state": "paused",
                "text": f"Paused {job.id} ({job.name})."}  # fmt: skip

    async def resume(self, args: dict[str, Any], call: CallContext | None = None) -> dict[str, Any]:
        del call
        job = self._job(RESUME_JOB.name, args)
        if job.state == "active":
            return {"id": job.id, "resumed": False, "state": "active",
                    "text": f"{job.id} ({job.name}) is already active."}  # fmt: skip
        now = self.ctx.clock()
        store.set_state(self.ctx.conn, job.id, "active", now, expect=("paused",))
        resumed = store.get(self.ctx.conn, job.id)
        assert resumed is not None
        nxt = self._describe(resumed)["next_due"]
        tail = f" Next: {nxt}." if nxt else ""
        return {"id": job.id, "resumed": True, "state": "active", "next_due": nxt,
                "text": f"Resumed {job.id} ({job.name}).{tail}"}  # fmt: skip

    async def delete(self, args: dict[str, Any], call: CallContext | None = None) -> dict[str, Any]:
        del call
        job = self._job(DELETE_JOB.name, args)
        store.set_state(
            self.ctx.conn, job.id, "deleted", self.ctx.clock(), expect=("active", "paused")
        )
        return {"id": job.id, "deleted": True, "text": f"Deleted {job.id} ({job.name})."}

    async def run_now(
        self, args: dict[str, Any], call: CallContext | None = None
    ) -> dict[str, Any]:
        by, _ = self._caller(call)
        job = self._job(RUN_JOB_NOW.name, args)
        if job.state != "active":
            return {"id": job.id, "started": False,
                    "text": f"{job.id} ({job.name}) is paused; resume it first."}  # fmt: skip
        if rj.busy(self.ctx.conn, job):
            return {"id": job.id, "started": False,
                    "text": f"{job.id}'s last run {job.last_run_code} is still going."}  # fmt: skip
        components = self.ctx.state.components
        runner = cast(
            "rj.JobRunner | None",
            components.runners.get(rj.RUNNER) if components is not None else None,
        )
        channel: Any = getattr(runner, "channel", None)  # the runner's: the daemon's hub
        ok, text = await rj.start(
            self.ctx.conn,
            job,
            runner=runner,
            principals=self.ctx.principals,
            channel=channel,
            now=self.ctx.clock(),
            manual=True,
            by=by,
        )
        out: dict[str, Any] = {"id": job.id, "started": ok, "text": text}
        if ok:
            fresh = store.get(self.ctx.conn, job.id)
            out["code"] = None if fresh is None else fresh.last_run_code
        return out


class _Handler(ContextHandler):
    def __init__(self, fn: Any) -> None:
        self.fn = fn

    async def __call__(
        self, args: dict[str, Any], ctx: CallContext | None = None
    ) -> dict[str, Any]:
        return cast(dict[str, Any], await self.fn(args, ctx))


def handlers(ctx: BuiltinContext) -> dict[str, ToolHandler]:
    t = _Tools(ctx)
    return {
        ADD_JOB.name: cast(ToolHandler, _Handler(t.add)),
        LIST_JOBS.name: cast(ToolHandler, _Handler(t.list)),
        PAUSE_JOB.name: cast(ToolHandler, _Handler(t.pause)),
        RESUME_JOB.name: cast(ToolHandler, _Handler(t.resume)),
        DELETE_JOB.name: cast(ToolHandler, _Handler(t.delete)),
        RUN_JOB_NOW.name: cast(ToolHandler, _Handler(t.run_now)),
    }
