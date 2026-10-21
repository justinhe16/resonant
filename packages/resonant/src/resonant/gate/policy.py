"""PolicyGate: the Phase 2 gate. It weighs the risk (registered level) against who chose
the action (``Intent.origin``).

Evaluation order:
1. Registration: the intent's spec must be the one registered for (extension, tool).
2. Permission: ``can(principal, "request", extension)``.
3. Unsafe args (control, bidi, zero-width characters): Deny, so what runs always matches
   what was shown. Then the tool's ``refusals`` hook, if it has one: a call it names is
   denied outright, never asked (the shell family's never-list, PER-258).
4. dry_run: a tool that is neither an L0 read nor store-internal
   (``tools.is_store_internal``) becomes ``DryRun(would=<summary>)``.
5. Level x origin x autonomy (``rule``). ``scheduled`` is never explicit; ``tainted``
   (the model chose it after reading untrusted content, docs/brain-plan.md D6) asks for
   everything above L0. The gate never exempts anything: ToolCore sends a tainted call to a
   ``taint_exempt`` spec (notes, reminders, facts; PER-245) as ``inferred``:

   | level | autonomy on, explicit | on, inferred / scheduled | on, tainted   | autonomy off |
   |-------|-----------------------|--------------------------|---------------|--------------|
   | L0    | Allow                 | Allow                    | Allow         | Allow        |
   | L3    | Allow                 | Allow                    | ask           | ask          |
   | L2    | Allow                 | ask                      | ask           | ask          |
   | L1    | ask with code         | ask with code            | ask with code | ask with code|

Standing allows (GT-1, ``standing``: ``resonant.trust.tools.StandingAllows``): an owner's
own ``inferred`` call of an L2 tool that the owner answered ``yes always`` for before (an
``allow`` trust rule on ``tool:<name>``) runs as L3. Never for anyone else, never for L1,
never for ``explicit``, ``scheduled`` or ``tainted`` calls (they don't need it, or never get
it: a tainted call's prompt offers no ``yes always`` either), and
never under the kill switch or dry run (both are checked first). Its first prompt offers the
rule: ``(yes always = don't ask before running <tool> in <scope>)``; the scope and action are
stored on the approval, and ``resolve_reply`` saves exactly that rule in the same
transaction as the approval when the owner answers ``yes always``.

Kill switch: kv ``autonomy`` is read on every call, and anything but ``"on"`` (missing or
garbage) counts as off. ``evaluate`` never returns ``Veto`` yet.

Approvers (``approvers``): a builtin (``extension=None``) is owner-only. An extension tool's
approvers are the owner plus each principal its manifest names (``approvers_for``) that
holds ``approve`` for that extension in principals.yaml. The grant check runs here, so a
manifest can't grant itself approvers: a name without the grant, or unknown, is dropped
and logged.

Approvals, per (task, intent hash):
- "ask" reuses the pending approval if there is one, else inserts a new one (approvers as
  above; ``expires_at`` = now + ``approval_ttl_s``).
- Approved and unused: ``Allow(token, approval_id)``. The executor marks it used in the
  same transaction as its ``started`` row, so an approval runs at most once.
- Rejected, expired, or already used: Deny with ``NOT_APPROVED``, ``APPROVAL_EXPIRED`` or
  ``APPROVAL_USED``. A rejection also beats a later Allow for the same intent.

Text replies (``resolve_reply``): the actor must be an approver with ``approve``, the reply an
iMessage parser match, and the approval's prompt sent to the actor before the reply's
``occurred_at`` (the sent-before rule). A plain yes/no binds only when exactly one approval
is pending for the actor, now and when the reply was sent. ``require_code`` needs
``yes <code>``; a plain no may deny. ``yes always`` binds the same way but only to an
approval that offered it (``allow_always``: a worker's trust check or an owner's L2 chat
call; never ``require_code``) and only from the owner, else ``always_unsupported``.
Resolution is idempotent per reply event (``resolved_event_id``).

Batches (BA-1, ``open_batch``): a task that collected several changes needing approval asks
the owner once. ``open_batch`` stores the snapshot in ``approval_batches`` (its approvals, in
order), gives them one expiry and returns the batch, whose code is its first approval's. For
binding, a batch is one unit: its code is the only one that names it, a plain yes/no counts
it as one pending item, it needs the code when any of its approvals does (the strictest
level), ``yes always`` is refused for it, and only the owner it was asked of can answer it
(a member who approves one of its extensions gets ``nothing_pending``). Resolving it
resolves every approval in it in one transaction (one refused rolls back the rest), by text
or by id. ``yes 1,3 <code>`` (PER-255, text only) approves those changes and rejects the
others; the code is needed when any picked change needs it (the strictest of the picks), and
numbers that aren't the batch's changes, or picks for a lone approval, are refused.

Expiry is lazy: every entry point first runs ``expire_due``. Every ``evaluate`` writes one
``gate.evaluate`` audit row; every resolution attempt writes one ``approval.resolve`` row.
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol

from resonant.gate import actions as action_ids
from resonant.gate.codes import clean_external, new_code
from resonant.gate.summary import phrased, summarize, unsafe_args
from resonant.gate.types import (
    Allow,
    ApprovalBatch,
    ApprovalReply,
    ApprovalResolution,
    Decision,
    Deny,
    DryRun,
    Intent,
    NeedsApproval,
    Origin,
    PendingApproval,
    ReplyRefusal,
    ResolveResult,
)
from resonant.principals import Principals
from resonant.store import approvals as ap
from resonant.store import outbound
from resonant.store import trust as trust_store
from resonant.store.audit import audit
from resonant.store.db import atomic, kv_get, utcnow
from resonant.store.tasks import insert_event
from resonant.tools import ToolRegistry, is_store_internal
from resonant.tools.registry import ToolKey
from resonant_sdk import Effect, Event, Level

log = logging.getLogger(__name__)

# Deny reasons the runner maps to its templated replies.
NOT_APPROVED = "not approved"
APPROVAL_EXPIRED = "approval expired"
APPROVAL_USED = "approval already used"
NO_TASK = "approvals need a task"

# Channels whose text replies may bind approvals. The iMessage reader already drops SMS.
REPLY_CHANNELS = frozenset({"imessage"})

# BA-1: at most this many changes wait on the owner in one batch (``gate.batch_max``).
DEFAULT_BATCH_MAX = 10
ALWAYS_NOTE = " (yes always = "  # how a standing allow's offer starts in a summary
BATCH_SUMMARY_CHARS = 200  # a batch as one line (``pending_for``: /approvals, re-asks)

Rule = Literal["allow", "ask", "ask_code"]

# A code-built heading for a tool's approval summary (e.g. "delete all 4 notes"). It must
# never come from a model; ``summarize`` cleans it anyway.
SummaryAction = Callable[[Intent], str | None]

# A tool's level for one call, when it depends on the call (``merge_run`` follows the trust
# rules). None keeps the registered level. Only L2 and L3 may be chosen, and only for a tool
# registered at one of them: an override never makes a tool skip the L1 code or the L0 path.
LevelFor = Callable[[Intent], Level | None]

# Why one call is never allowed (denied, never asked), or None: the shell family's never-list
# (PER-258, ``gate/shell.py``). Code-built; it becomes the Deny reason.
RefusalFor = Callable[[Intent], str | None]

# The approver names an extension's manifest declares (``Manifest.approvers``). Untrusted:
# the gate keeps only names that hold the approve grant for that extension.
ApproversFor = Callable[[str], Sequence[str]]


class StandingScope(Protocol):
    """What a ``yes always`` on one call would save (``trust.tools.ToolScope``)."""

    @property
    def scope(self) -> str: ...

    @property
    def action(self) -> str: ...

    @property
    def text(self) -> str: ...


class Standing(Protocol):
    """The owner's standing allows for chat tools (``trust.tools.StandingAllows``)."""

    def allows(self, intent: Intent) -> object | None:
        """The rule that lets this call run without asking, or None."""
        ...

    def offer(self, intent: Intent) -> StandingScope | None:
        """The rule a ``yes always`` on this call's prompt would save, or None."""
        ...


def no_manifest_approvers(_extension: str) -> Sequence[str]:
    """The default until the extension host supplies manifests: nobody but the owner."""
    return ()


def rule(level: Level, origin: Origin, autonomy_on: bool) -> Rule:
    """The D1/D7 table: risk (level) x who chose it (origin) x the kill switch. Only
    ``explicit`` is consent; ``inferred`` and ``scheduled`` ask before L2; ``tainted`` asks
    before L3 too (an L0 read still runs, an L1 still needs its code)."""
    if level is Level.L0:
        return "allow"
    if level is Level.L1:
        return "ask_code"
    if not autonomy_on or origin == "tainted":
        return "ask"
    if level is Level.L3 or origin == "explicit":
        return "allow"
    return "ask"


def autonomy_on(conn: sqlite3.Connection) -> bool:
    """The kill switch. Fails closed: a missing or unreadable value is off."""
    try:
        return kv_get(conn, "autonomy") == "on"
    except (json.JSONDecodeError, TypeError, ValueError):
        return False


class PolicyGate:
    def __init__(
        self,
        conn: sqlite3.Connection,
        principals: Principals,
        registry: ToolRegistry,
        *,
        dry_run: bool,
        approval_ttl_s: float,
        actions: Mapping[ToolKey, SummaryAction] | None = None,
        approvers_for: ApproversFor = no_manifest_approvers,
        levels: Mapping[ToolKey, LevelFor] | None = None,
        standing: Standing | None = None,
        clock: Callable[[], datetime] = utcnow,
        batch_max: int = DEFAULT_BATCH_MAX,
        refusals: Mapping[ToolKey, RefusalFor] | None = None,
        summaries: Mapping[ToolKey, SummaryAction] | None = None,
    ) -> None:
        """``refusals``: per-call never-lists; ``summaries``: a tool's whole approval line,
        code-built (the shell family's ``run `<command>` in <dir>``), cleaned like any
        other."""
        self.conn = conn
        self.principals = principals
        self.registry = registry
        self.dry_run = dry_run
        self.ttl = timedelta(seconds=approval_ttl_s)
        self.actions = dict(actions or {})
        self.approvers_for = approvers_for
        self.levels = dict(levels or {})
        self.standing = standing
        self.clock = clock
        self.batch_max = batch_max
        self.refusals = dict(refusals or {})
        self.summaries = dict(summaries or {})
        self._tokens: dict[str, str] = {}

    @property
    def owner(self) -> str:
        return self.principals.owner

    # --- evaluate ----------------------------------------------------------------------

    def evaluate(self, intent: Intent, *, capped: bool = False) -> Decision:
        """The decision for ``intent``. ``capped`` (PER-261, the tool core): the task already
        ran its ``MAX_SIDE_EFFECTS`` changes without asking, so a change this would allow
        asks instead (it joins the task's batch). A read, and what is refused, are unchanged."""
        now = self.clock()
        with atomic(self.conn):
            ap.expire_due(self.conn, now)
            autonomy = autonomy_on(self.conn)
            decision, standing = self._decide(intent, now, autonomy, capped=capped)
            audit(
                self.conn,
                "gate.evaluate",
                task_id=intent.task_id,
                requested_by=intent.principal,
                extension=intent.extension,
                tool=intent.tool,
                effect=intent.effect.value,
                level=intent.level.value,
                intent_hash=intent.hash,
                decision=type(decision).__name__,
                trace_id=intent.trace_id,
                origin=intent.origin,
                autonomy="on" if autonomy else "off",
                dry_run=self.dry_run,
                approval_id=(
                    decision.approval_id if isinstance(decision, Allow | NeedsApproval) else None
                ),
                reason=decision.reason if isinstance(decision, Deny) else None,
                standing=standing,
                capped=capped or None,
                args=intent.args,
            )
        return decision

    def _decide(
        self, intent: Intent, now: datetime, autonomy: bool, *, capped: bool = False
    ) -> tuple[Decision, bool | None]:
        """The decision, and whether a standing allow made the call L3 (None: not asked)."""
        if self.registry.get(intent.extension, intent.tool) != intent.spec:
            # Static reasons: the audit row already names the tool, extension and principal.
            return Deny("tool is not registered with this spec"), None
        if not self.principals.may_request(intent.principal, intent.spec):
            return Deny("not permitted to request this tool"), None
        unsafe = unsafe_args(intent.args)
        if unsafe:
            # Never echo the offending keys or values: they are what's unsafe to show.
            return Deny(f"args contain unsafe characters ({len(unsafe)} found)"), None
        refuse = self.refusals.get((intent.extension, intent.tool))
        if refuse is not None and (why := refuse(intent)) is not None:
            return Deny(why), None
        l0_read = intent.effect is Effect.READ and intent.level is Level.L0
        if self.dry_run and not l0_read and not is_store_internal(intent.spec):
            return DryRun(would=self._summary(intent)), None
        level = self._level(intent)
        standing: bool | None = None
        if level is Level.L2 and autonomy and self._standing_eligible(intent):
            assert self.standing is not None
            standing = self.standing.allows(intent) is not None
            if standing:
                level = Level.L3
        verdict = rule(level, intent.origin, autonomy)
        if capped and verdict == "allow" and not l0_read:
            verdict = "ask"  # past the task's unasked changes: this one asks (no yes always)
        if verdict == "allow" and intent.level is Level.L0:
            return self._allow(intent), standing
        if ap.was_rejected(self.conn, intent.task_id, intent.hash):
            return Deny(NOT_APPROVED), standing  # the owner's "no" holds for this intent
        last = ap.latest_for_intent(self.conn, intent.task_id, intent.hash)
        if verdict == "allow":
            # An approval already given for this intent is still spent (single use), e.g.
            # the yes always that just made the call L3.
            approved = ap.approved_unused(self.conn, intent.task_id, intent.hash)
            return self._allow(intent, approved.id if approved else None), standing
        offer = None
        if (
            verdict == "ask"
            and not capped
            and level is Level.L2
            and autonomy
            and self._standing_eligible(intent)
        ):
            assert self.standing is not None
            offer = self.standing.offer(intent)
        decision = self._ask(intent, now, last, require_code=verdict == "ask_code", offer=offer)
        return decision, standing

    def _standing_eligible(self, intent: Intent) -> bool:
        """A standing allow may apply: the owner's own model-chosen call of a tool at L2."""
        return (
            self.standing is not None
            and intent.origin == "inferred"
            and intent.principal == self.principals.owner
            and intent.level is not Level.L1
        )

    def _level(self, intent: Intent) -> Level:
        """The registered level, or the tool's ``levels`` choice between L2 and L3 for this
        call (see ``LevelFor``)."""
        level = intent.level
        pick = self.levels.get((intent.extension, intent.tool))
        if pick is None or level not in (Level.L2, Level.L3):
            return level
        chosen = pick(intent)
        return chosen if chosen is Level.L2 or chosen is Level.L3 else level

    def _summary(self, intent: Intent) -> str:
        """The code-built summary, with the tool's action heading if it has one."""
        whole = self.summaries.get((intent.extension, intent.tool))
        if whole is not None and (line := whole(intent)):
            return clean_external(line) or intent.tool
        action = self.actions.get((intent.extension, intent.tool))
        return summarize(intent, action=action(intent) if action else None)

    def _phrased(self, intent: Intent, summary: str) -> bool:
        """The summary is the tool's own template, filled (no whole line or action heading)."""
        whole = self.summaries.get((intent.extension, intent.tool))
        if whole is not None and whole(intent):
            return False
        action = self.actions.get((intent.extension, intent.tool))
        if action is not None and action(intent):
            return False
        return summary == phrased(intent)

    def approvers(self, extension: str | None) -> tuple[str, ...]:
        """Who may approve a call to this extension's tools: the owner first, then each
        manifest-named principal that holds ``approve`` for it. Builtins are owner-only."""
        owner = self.principals.owner
        if extension is None:
            return (owner,)
        named = self.approvers_for(extension)
        if isinstance(named, str):  # a bare name is a seam bug, never a list of letters
            raise TypeError("approvers_for must return a sequence of names, not a str")
        found = [owner]
        for name in named:
            if name in found:
                continue
            if not self.principals.can(name, "approve", extension):
                log.warning("dropping approver %r: no approve grant for %r", name, extension)
                continue
            found.append(name)
        return tuple(found)

    def _allow(self, intent: Intent, approval_id: str | None = None) -> Allow:
        token = secrets.token_urlsafe(16)
        self._tokens[token] = intent.hash
        return Allow(token=token, approval_id=approval_id)

    def _ask(
        self,
        intent: Intent,
        now: datetime,
        last: ap.Approval | None,
        *,
        require_code: bool,
        offer: StandingScope | None = None,
    ) -> Decision:
        approved = ap.approved_unused(self.conn, intent.task_id, intent.hash)
        if approved is not None:
            return self._allow(intent, approved.id)
        pending = ap.pending_for_intent(self.conn, intent.task_id, intent.hash, now)
        if pending is not None:
            return _needs(pending)
        if last is not None:
            return Deny(APPROVAL_USED if last.status == "approved" else APPROVAL_EXPIRED)
        task = self.conn.execute("SELECT 1 FROM tasks WHERE id = ?", (intent.task_id,))
        if task.fetchone() is None:
            return Deny(NO_TASK)  # e.g. the router's fast path: nothing could wait
        # PER-269: one request per action. Another path (a run's check, another task) is
        # already asking for it: point to that one instead of opening a second.
        action = action_ids.for_intent(self.conn, intent)
        if action is not None and (held := ap.pending_for_action(self.conn, action.key, now)):
            return Deny(action_ids.already_asking(self.conn, held[0]))
        summary = self._summary(intent)
        said = self._phrased(intent, summary)  # PER-268: "OK to merge PR #121 in …? [P3] yes / no"
        always = offer is not None and not require_code
        if always:
            assert offer is not None
            summary = f"{summary} (yes always = {offer.text})"
        spec = ap.NewApproval(
            task_id=intent.task_id,
            intent_hash=intent.hash,
            tool=intent.tool,
            effect=intent.effect,
            level=intent.level,
            summary=summary,
            approvers=self.approvers(intent.extension),
            code=new_code(ap.pending_codes(self.conn)),
            expires_at=now + self.ttl,
            require_code=require_code,
            extension=intent.extension,
            requested_by=intent.principal,
            allow_always=always,
            always_scope=offer.scope if always and offer is not None else None,
            always_action=offer.action if always and offer is not None else None,
            phrased=said,
            action_key=action.key if action is not None else None,
            action_target=action.target if action is not None else None,
        )
        approval_id = ap.insert_approval(self.conn, spec, now)
        created = ap.get_approval(self.conn, approval_id)
        assert created is not None
        return _needs(created)

    def consume(self, token: str, intent_hash: str) -> bool:
        return self._tokens.pop(token, None) == intent_hash

    # --- approvals ---------------------------------------------------------------------

    def pending_for(self, principal: str) -> Sequence[PendingApproval]:
        """The principal's pending approvals, a batch as one entry (its code, its first
        approval's id, a one-line summary of all of it, its strictest level)."""
        now = self.clock()
        with atomic(self.conn):
            ap.expire_due(self.conn, now)
            rows = ap.pending_for(self.conn, principal, now)
            units = self._units(rows, principal)
        return tuple(u.pending() for u in units)

    def approval_states(self, approval_ids: Sequence[str]) -> dict[str, ap.ApprovalStatus]:
        """BA-1: each approval's status now (after lazy expiry); a missing one is left out."""
        now = self.clock()
        out: dict[str, ap.ApprovalStatus] = {}
        with atomic(self.conn):
            ap.expire_due(self.conn, now)
            for approval_id in approval_ids:
                row = ap.get_approval(self.conn, approval_id)
                if row is not None:
                    out[approval_id] = row.status
        return out

    def drop(self, approval_ids: Sequence[str], *, reason: str) -> None:
        """BA-1: expire these pending approvals (a batch that can't be asked as one; a change
        past the batch cap), so no reply can bind them. Audited ``approval.expired``."""
        now = self.clock()
        with atomic(self.conn):
            for approval_id in approval_ids:
                ap.expire_one(self.conn, approval_id, now, reason=reason)

    def open_batch(self, principal: str, approval_ids: Sequence[str]) -> ApprovalBatch:
        """Ask ``principal`` (the owner) about these pending approvals of one task as one
        batch: the snapshot is stored (``approval_batches``, in order) and they get one expiry,
        ``now + approval_ttl_s``. The same set again returns the batch already open (a
        re-prompt), unchanged. ``ValueError`` for fewer than two or more than ``batch_max``
        approvals, anyone but the owner, an approval that isn't pending, another task's, one
        the owner can't approve, or one already in another batch."""
        ids = tuple(dict.fromkeys(approval_ids))
        if len(ids) < 2:
            raise ValueError("a batch has at least two approvals")
        if len(ids) > self.batch_max:
            raise ValueError(f"a batch has at most {self.batch_max} approvals")
        if principal != self.principals.owner:
            raise ValueError("batches are asked of the owner")
        now = self.clock()
        with atomic(self.conn):
            ap.expire_due(self.conn, now)
            rows = [ap.get_approval(self.conn, i) for i in ids]
            found = [r for r in rows if r is not None]
            if len(found) != len(ids) or any(r.status != "pending" for r in found):
                raise ValueError("every approval in a batch must be pending")
            if len({r.task_id for r in found}) != 1 or found[0].task_id is None:
                raise ValueError("a batch is one task's approvals")
            if not all(
                principal in r.approvers and self.principals.can(principal, "approve", r.extension)
                for r in found
            ):
                raise ValueError("the owner can't approve every approval in it")
            index = ap.batches_of(self.conn, ids)
            if index:
                existing = index.get(ids[0])
                same = existing is not None and existing.approval_ids == ids
                if not same or existing is None or existing.used_at is not None:
                    raise ValueError("an approval is already in another batch")
                return _Unit(tuple(found), existing).as_batch()
            row = ap.insert_batch(self.conn, principal, ids, now)
            ap.set_expiry(self.conn, ids, now + self.ttl)
            members = tuple(a for a in (ap.get_approval(self.conn, i) for i in ids) if a)
            unit = _Unit(members, row)
            audit(
                self.conn,
                "approval.batch",
                task_id=found[0].task_id,
                requested_by=principal,
                batch_id=row.id,
                approval_ids=list(ids),
                code=unit.code,
                level=unit.level.value,
                require_code=unit.require_code,
            )
        return unit.as_batch()

    def _units(self, rows: Sequence[ap.Approval], actor: str) -> list[_Unit]:
        """``rows`` (open approvals, oldest first) as what one reply can answer: each lone
        approval, and each batch whose approvals are all in ``rows`` and that was asked of
        ``actor``, in place of its first approval. A batch asked of someone else, or with an
        approval missing from ``rows``, is left out: ``actor`` can't answer it."""
        index = ap.batches_of(self.conn, [a.id for a in rows])
        by_id = {a.id: a for a in rows}
        out: list[_Unit] = []
        seen: set[str] = set()
        for a in rows:
            batch = index.get(a.id)
            if batch is None:
                out.append(_Unit((a,)))
                continue
            if batch.id in seen:
                continue
            seen.add(batch.id)
            members = tuple(by_id.get(i) for i in batch.approval_ids)
            if batch.principal != actor or any(m is None for m in members):
                continue
            out.append(_Unit(tuple(m for m in members if m is not None), batch))
        return out

    def _resolve_unit(
        self,
        unit: _Unit,
        *,
        approved: bool,
        actor: str,
        now: datetime,
        event_id: str | None = None,
        always: bool = False,
        picks: tuple[int, ...] | None = None,
    ) -> str | None:
        """Resolve a lone approval, or every approval of a batch (one transaction: one refused
        rolls the others back). ``picks`` (a batch's change numbers, PER-255): approve only
        those and reject the rest. None on success, else the store's reason."""
        if unit.batch is None:
            return ap.resolve(
                self.conn, unit.head.id, approved=approved, decided_by=actor, now=now,
                event_id=event_id, always=always,
            )  # fmt: skip
        try:
            with atomic(self.conn):
                for n, m in enumerate(unit.members, 1):
                    yes = approved and (picks is None or n in picks)
                    why = ap.resolve(
                        self.conn, m.id, approved=yes, decided_by=actor, now=now,
                        event_id=event_id,
                    )  # fmt: skip
                    if why is not None:
                        raise _BatchRefused(why)
                ap.mark_batch_used(self.conn, unit.batch.id, now)
        except _BatchRefused as e:
            return e.reason
        return None

    def resolve(self, resolution: ApprovalResolution) -> ResolveResult:
        """Resolve one approval by id (CLI or button) and wake its task, in one transaction.
        An approval in an open batch resolves the whole batch (one decision for it)."""
        now = self.clock()
        with atomic(self.conn):
            ap.expire_due(self.conn, now)
            approval = ap.get_approval(self.conn, resolution.approval_id)
            reason: str | None
            unit: _Unit | None = None
            if approval is None or approval.task_id is None:
                reason = "not_found"
            elif resolution.actor not in approval.approvers or not self.principals.can(
                resolution.actor, "approve", approval.extension
            ):
                reason = "not an approver"
            else:
                unit = self._unit_of(approval)
                if unit.batch is not None and unit.batch.principal != resolution.actor:
                    reason = "not an approver"  # a batch is the owner's to answer
                elif resolution.always and (
                    unit.batch is not None
                    or not approval.allow_always
                    or approval.require_code
                    or resolution.actor != self.principals.owner
                ):
                    reason = "always_unsupported"  # as for "yes always" by text
                else:
                    reason = self._resolve_unit(
                        unit, approved=resolution.approved, actor=resolution.actor, now=now,
                        always=resolution.always,
                    )  # fmt: skip
            # A non-approver can't tell an existing approval from a missing one.
            shown = "not_found" if reason == "not an approver" else reason
            result = ResolveResult(ok=False, reason=shown)
            if reason is None and unit is not None:
                done = [ap.get_approval(self.conn, m.id) for m in unit.members]
                result = ap.resolve_result([d for d in done if d is not None])
                head = done[0]
                if (
                    unit.batch is None
                    and head is not None
                    and head.always
                    and head.always_scope
                    and head.always_action
                ):
                    _add_standing_rule(self.conn, head, resolution.actor, now)
                ap.wake_resolved(self.conn, result, now, principal=resolution.actor)
            audit(
                self.conn,
                "approval.resolve",
                task_id=approval.task_id if approval else None,
                requested_by=resolution.actor,
                approved_by=resolution.actor if result.ok else None,
                tool=approval.tool if approval else None,
                extension=approval.extension if approval else None,
                intent_hash=approval.intent_hash if approval else None,
                decision=("approved" if resolution.approved else "rejected")
                if result.ok
                else "refused",
                requested="approve" if resolution.approved else "deny",
                approval_id=resolution.approval_id,
                batch_id=unit.batch.id if unit is not None and unit.batch is not None else None,
                approval_ids=[m.id for m in unit.members] if unit is not None else None,
                actor_identity=resolution.actor_identity,
                path=resolution.path,
                comments=resolution.comments,
                always=resolution.always,
                ok=result.ok,
                reason=reason,
            )
        return result

    def _unit_of(self, approval: ap.Approval) -> _Unit:
        """``approval`` alone, or the open batch it is in (its approvals, in order)."""
        batch = ap.batches_of(self.conn, [approval.id]).get(approval.id)
        if batch is None or batch.used_at is not None:
            return _Unit((approval,))
        members = tuple(
            a for a in (ap.get_approval(self.conn, i) for i in batch.approval_ids) if a is not None
        )
        return _Unit(members, batch)

    def resolve_reply(self, reply: ApprovalReply) -> ResolveResult:
        """Bind a text reply to one approval, resolve it and wake its task, in one
        transaction. See ``ApprovalReply`` for the binding rules and ``ReplyRefusal`` for
        the closed set of refusal reasons.

        Idempotent per event: if ``reply.event_id`` already resolved an approval, this
        returns that result again with no new effect (no write, wake or audit).
        """
        now = self.clock()
        with atomic(self.conn):
            ap.expire_due(self.conn, now)
            earlier = ap.by_resolved_event(self.conn, reply.event_id)
            if earlier:
                if all(a.decided_by == reply.actor for a in earlier):
                    return ap.resolve_result(earlier)
                return ResolveResult(ok=False, reason="nothing_pending")
            unit, reason = self._bind(reply, now)
            target = unit.head if unit is not None else None
            if unit is not None:
                reason = self._resolve_unit(
                    unit,
                    approved=reply.decision == "approve",
                    actor=reply.actor,
                    now=now,
                    event_id=reply.event_id,
                    always=reply.always,
                    picks=reply.picks,
                )
            result = ResolveResult(ok=False, reason=reason)
            if reason is None and unit is not None:
                rows = [ap.get_approval(self.conn, m.id) for m in unit.members]
                done_rows = [d for d in rows if d is not None]
                result = ap.resolve_result(done_rows)
                done = done_rows[0]
                if reply.comments and reply.decision == "approve":
                    _add_comments(self.conn, done, reply)
                if unit.batch is None and done.always and done.always_scope and done.always_action:
                    _add_standing_rule(self.conn, done, reply.actor, now)
                ap.wake_resolved(self.conn, result, now, principal=reply.actor)
            audit(
                self.conn,
                "approval.resolve",
                task_id=target.task_id if target else None,
                requested_by=reply.actor,
                approved_by=reply.actor if result.ok and reply.decision == "approve" else None,
                tool=target.tool if target else None,
                extension=target.extension if target else None,
                intent_hash=target.intent_hash if target else None,
                decision=_decision(reply, result.ok),
                requested=reply.decision,
                approval_id=target.id if target else None,
                batch_id=unit.batch.id if unit is not None and unit.batch is not None else None,
                approval_ids=[m.id for m in unit.members] if unit is not None else None,
                picks=list(reply.picks) if reply.picks is not None else None,
                approved_ids=_picked(unit, reply.picks, True),
                rejected_ids=_picked(unit, reply.picks, False),
                actor_identity=reply.actor_identity,
                channel=reply.channel,
                path=reply.path,
                code=reply.code,
                event_id=reply.event_id,
                occurred_at=reply.occurred_at.isoformat(),
                raw_text=reply.raw_text,
                comments=reply.comments,
                always=reply.always,
                ok=result.ok,
                reason=reason,
            )
        return result

    def _bind(
        self, reply: ApprovalReply, now: datetime
    ) -> tuple[_Unit | None, ReplyRefusal | None]:
        """What this reply binds to (one approval, or a batch as one unit), or why it binds
        none."""
        if (
            reply.channel not in REPLY_CHANNELS
            or reply.path != "parser"
            or reply.all_in_batch
            or reply.batch_id is not None
        ):
            return None, "unsupported"
        mine = self._units(
            [a for a in ap.pending_for(self.conn, reply.actor, now) if self._may_approve(reply, a)],
            reply.actor,
        )
        if not mine:
            return None, "nothing_pending"  # also what a non-approver always gets
        # A plain reply is judged among the prompts the actor got: an approval pinned in a turn
        # that hasn't asked yet (BA-1) can't be answered and mustn't make it ambiguous.
        prompted = [u for u in mine if u.head.sent_at(reply.actor) is not None]
        if reply.code is not None:
            code = reply.code.upper()
            target = next((u for u in mine if u.code == code), None)
            if target is None:
                return None, "unknown_code"
        elif len(prompted) > 1 or (not prompted and len(mine) > 1):
            return None, "ambiguous"
        else:
            target = prompted[0] if prompted else mine[0]
        if not all(_sent_before(m, reply) for m in target.members):
            return None, "not_sent_before"
        if reply.picks is not None:
            if target.batch is None:
                return None, "not_a_batch"
            if any(n < 1 or n > len(target.members) for n in reply.picks):
                return None, "unknown_pick"
        if reply.always and (
            target.batch is not None
            or not target.head.allow_always
            or target.require_code
            or reply.actor != self.principals.owner
        ):
            # Never read as a plain yes: "always" asked for more than this approval offers
            # (a batch never offers it), and only the owner widens trust.
            return None, "always_unsupported"
        if reply.code is None:
            # Judge a plain reply against what the actor had been shown when they sent it,
            # so an old reply re-routed later can't bind what has since become the only one.
            opened = [
                a
                for a in ap.open_at(self.conn, reply.actor, reply.occurred_at)
                if self._may_approve(reply, a) and _sent_before(a, reply)
            ]
            seen = [u.head.id for u in self._units(opened, reply.actor)]
            if seen != [target.head.id]:
                return None, "ambiguous"
            if reply.decision == "approve" and (
                target.needs_code(reply.picks) or self._claude_since(target, reply)
            ):
                return None, "code_required"
        return target, None

    def _claude_since(self, unit: _Unit, reply: ApprovalReply) -> bool:
        """PER-261: a Claude answer (outside text, right above the reply) went out after this
        prompt or its reminder, so a bare yes, or yes 1,3, needs the code: the answer can't
        stand in for the prompt."""
        from resonant.gateway.channel import approval_dedupe_key  # the gateway imports the gate

        actor = reply.actor
        ids = [m.id for m in unit.members] + ([unit.batch.id] if unit.batch is not None else [])
        keys = {approval_dedupe_key(i, actor) for i in ids}
        keys |= {outbound.reminder_key(i, actor) for i in ids}
        return outbound.claude_since(self.conn, actor, keys, reply.occurred_at)

    def _may_approve(self, reply: ApprovalReply, a: ap.Approval) -> bool:
        return (
            a.task_id is not None
            and reply.actor in a.approvers
            and self.principals.can(reply.actor, "approve", a.extension)
        )


def _decision(reply: ApprovalReply, ok: bool) -> str:
    """A reply's ``approval.resolve`` decision: ``approved``, ``rejected``, ``partial`` (a
    ``yes 1,3`` that approved some of a batch and rejected the rest, PER-261) or ``refused``."""
    if not ok:
        return "refused"
    if reply.decision != "approve":
        return "rejected"
    return "partial" if reply.picks is not None else "approved"


def _picked(unit: _Unit | None, picks: tuple[int, ...] | None, approved: bool) -> list[str] | None:
    """For a ``yes 1,3`` reply's audit row (PER-255): the approvals it approves (``approved``)
    or rejects, so each approval's decision is on record. None for any other reply."""
    if unit is None or picks is None:
        return None
    return [m.id for n, m in enumerate(unit.members, 1) if (n in picks) == approved]


class _BatchRefused(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def strictest(levels: Sequence[Level]) -> Level:
    """L1 (the code) over L2 over L3 (L0 never asks)."""
    order = (Level.L1, Level.L2, Level.L3, Level.L0)
    return min(levels, key=order.index)


# Builtins that widen trust (loosening asks the owner for that change): in a batch they are
# marked, and the batch needs its code, so a plain yes never widens trust along with the rest.
WIDENS_TRUST: frozenset[str] = frozenset({"allow_tools", "allow_trust_rule", "remove_trust_rule"})


def widens_trust(tool: str, extension: str | None) -> bool:
    return extension is None and tool in WIDENS_TRUST


def batch_line(summary: str, *, offered: bool, widens: bool = False) -> str:
    """An approval's summary as a batch line: without the ``yes always`` offer its prompt
    would have carried (a batch never offers one). Only an approval that ``offered`` it has
    one, as the summary's code-built suffix, so only that suffix is cut: model-chosen args can
    never shorten the line. ``widens``: a trust-widening change, marked."""
    if offered and summary.endswith(")") and ALWAYS_NOTE in summary:
        summary = summary.rsplit(ALWAYS_NOTE, 1)[0]
    return f"widens trust: {summary}" if widens else summary


def change_line(a: ap.Approval) -> str:
    """``a`` as one line of its batch's prompt (also the dashboard's batch row, PER-254)."""
    return batch_line(a.summary, offered=a.allow_always, widens=widens_trust(a.tool, a.extension))


@dataclass(frozen=True)
class _Unit:
    """What one reply answers: a lone approval, or every approval of a batch (BA-1)."""

    members: tuple[ap.Approval, ...]
    batch: ap.BatchRow | None = None

    @property
    def head(self) -> ap.Approval:
        return self.members[0]

    @property
    def code(self) -> str | None:
        return self.head.code

    @property
    def require_code(self) -> bool:
        """Any approval needs its code, or (a batch) one of them widens trust."""
        return self.needs_code(None)

    def needs_code(self, picks: tuple[int, ...] | None) -> bool:
        """Whether approving ``picks`` (a batch's change numbers; None: all of it) needs the
        code: the strictest of those changes (L1, pay), or (a batch) one of them widens trust."""
        chosen = [m for n, m in enumerate(self.members, 1) if picks is None or n in picks]
        if self.batch is not None and any(widens_trust(m.tool, m.extension) for m in chosen):
            return True
        return any(m.require_code for m in chosen)

    @property
    def level(self) -> Level:
        return strictest([m.level for m in self.members])

    @property
    def destructive(self) -> bool:
        return any(m.level is Level.L1 and m.effect is not Effect.PAY for m in self.members)

    @property
    def effect(self) -> Effect:
        effects = {m.effect for m in self.members}
        return Effect.PAY if Effect.PAY in effects else self.head.effect

    def pending(self) -> PendingApproval:
        a = self.head
        if self.batch is None:
            return PendingApproval(
                approval_id=a.id,
                code=a.code or "",
                task_id=a.task_id or "",
                summary=a.summary,
                effect=a.effect,
                level=a.level,
                require_code=a.require_code,
                expires_at=a.expires_at,
            )
        summary = f"{len(self.members)} changes: " + "; ".join(change_line(m) for m in self.members)
        if len(summary) > BATCH_SUMMARY_CHARS:
            summary = summary[: BATCH_SUMMARY_CHARS - 1].rstrip() + "…"
        return PendingApproval(
            approval_id=a.id,
            code=a.code or "",
            task_id=a.task_id or "",
            summary=summary,
            effect=self.effect,
            level=self.level,
            require_code=self.require_code,
            expires_at=min(m.expires_at for m in self.members),
            changes=len(self.members),
        )

    def as_batch(self) -> ApprovalBatch:
        assert self.batch is not None
        return ApprovalBatch(
            batch_id=self.batch.id,
            principal=self.batch.principal,
            approval_ids=self.batch.approval_ids,
            created_at=self.batch.created_at,
            code=self.code or "",
            task_id=self.head.task_id or "",
            expires_at=min(m.expires_at for m in self.members),
            lines=tuple(change_line(m) for m in self.members),
            require_code=self.require_code,
            destructive=self.destructive,
            level=self.level,
            effect=self.effect,
        )


def _sent_before(a: ap.Approval, reply: ApprovalReply) -> bool:
    """The sent-before rule: the prompt reached the actor before the reply was sent."""
    sent = a.sent_at(reply.actor)
    return sent is not None and sent < reply.occurred_at


def _add_comments(conn: sqlite3.Connection, a: ap.Approval, reply: ApprovalReply) -> None:
    """``yes with comments: ...``: a ``human_input`` event for the task, at most once."""
    assert a.task_id is not None
    row = conn.execute("SELECT trace_id FROM tasks WHERE id = ?", (a.task_id,)).fetchone()
    assert row is not None  # resolve just checked the task is waiting or running
    insert_event(
        conn,
        Event(
            source="gate",
            type="human_input",
            task_id=a.task_id,
            principal=reply.actor,
            payload={"approval_id": a.id, "comments": reply.comments},
            dedupe_key=f"approval:{a.id}:comments",
            occurred_at=reply.occurred_at,
            trace_id=row["trace_id"],
        ),
    )


def _add_standing_rule(conn: sqlite3.Connection, a: ap.Approval, actor: str, now: datetime) -> None:
    """``yes always`` on a tool approval: save the ``allow`` rule its prompt offered."""
    assert a.always_scope is not None and a.always_action is not None
    added = trust_store.add_rule(
        conn,
        scope=a.always_scope,
        action=a.always_action,
        effect="allow",
        created_from="yes always",
        now=now,
        note=f"yes always to {a.code}" if a.code else "yes always",
    )
    if not added.existed:
        audit(
            conn,
            "trust.rule_added",
            task_id=a.task_id,
            approved_by=actor,
            rule_id=added.rule.id,
            scope=added.rule.scope,
            action_kind=added.rule.action,
            effect=added.rule.effect,
            created_from="yes always",
            approval_id=a.id,
            tool=a.tool,
            extension=a.extension,
            replaced=trust_store.rule_fields(added.replaced),
        )


def _needs(a: ap.Approval) -> NeedsApproval:
    assert a.code is not None
    return NeedsApproval(
        approval_id=a.id,
        code=a.code,
        approvers=a.approvers,
        expires_at=a.expires_at,
        summary=a.summary,
        effect=a.effect,
        level=a.level,
        require_code=a.require_code,
        allow_always=a.allow_always,
        phrased=a.phrased,
    )
