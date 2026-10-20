"""The shell target family (PER-258): the Claude brain's own Bash, Edit and Write calls.

The brain session runs with Claude Code's full built-ins. A read on the pre-allowed list
(``READ_RULES``, Claude Code rule syntax on ``--allowedTools``) runs without asking anyone.
Anything else reaches Resonant's permission tool (``runners/ask_tools.py``), which asks
:meth:`ShellPolicy.judge` what the call is and sends it through ``ToolCore`` as one of two
builtin specs (``tools/shell.py``): ``shell_read`` (L0) or ``shell`` (a change). The gate
asks this module again, through its ``levels`` and ``refusals`` hooks, so the level and the
never-list are decided inside the gate, from the call's args alone.

**What a command is.** The command is split the way a shell would (``shlex``, operators and
redirects as their own tokens) into segments. A leading ``cd <dir> &&`` moves the working
directory (``git -C <dir>`` does the same for that segment). Then:

- **read** (L0): every segment is a known read (``git log/show/diff/status/...``, ``gh pr
  view/list/...``, ``ls``, ``cat``, ``rg``...) with no flag that writes or runs something
  (``git diff --output``, ``rg --pre``, ``find -delete/-exec/-fprint``), no command or
  process substitution, no heredoc, no redirect except to ``/dev/null``, no ``VAR=value``
  prefix, and every path it names (and its working directory) inside the read roots and
  off the deny list. A read that names a path outside them is refused, never asked: the
  same rule as the read tools.
- **never** (refused outright, never asked): ``resonant secret ...``, ``resonant ext
  add|update``, ``security`` (the login keychain), a write under ``RESONANT_HOME`` (except
  the brain's own scratch dir), ``kill``/``pkill``/``killall`` of the daemon (or of every
  process: ``kill -1``), and ``launchctl`` stopping or removing it. Checked in every
  segment, inside ``sh -c``/``eval`` strings and behind wrappers (``env``, ``sudo``,
  ``xargs``, ``uv run``...).
- **L3** (runs, reports; asks when the turn is tainted): one simple command that is local
  branch work (``git add``, ``commit``, ``fetch``, ``switch``, ``stash``, ``checkout -b``,
  a new branch or tag), ``git push`` of a branch that isn't the default one (no force, no
  delete), ``gh pr create``, and an Edit or Write in a linked git worktree or a scratch dir.
- **L2** (asks the owner, a plain yes with a code; no ``yes always``): everything else.
  That includes any compound command that writes (``git log && git push``), a push to the
  default branch, ``gh pr merge``, ``rm``, ``launchctl``, ``resonant ...``, ``npm``,
  ``uv``, scripts and anything unrecognised.

The verb (``git-push``, ``gh-pr-create``, ``edit``, ``compound``...) and the repo name are in
the call's args: a standing allow is ``allow tool:shell.<verb> in <repo>``
(``trust.tools.StandingAllows``).

Classification is conservative by design: when in doubt a command is a write, and a write
that might be compound is L2. Nothing here runs a command (git facts are read from the
files under ``.git``, never by running git in a repo whose config could run code).
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shlex
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from resonant_sdk import Level

Kind = Literal["read", "write", "never"]

TOOL_WRITE = "shell"  # the builtin specs (tools/shell.py)
TOOL_READ = "shell_read"
FILE_TOOLS: Mapping[str, str] = {
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
}
OWN_READS = ("Read", "Grep", "Glob")  # Claude Code asks for these only outside the dirs
WEB_TOOLS = ("WebSearch", "WebFetch")
SHOWN_CHARS = 160
GLOB_MAX = 200

# Claude Code rules for ``--allowedTools``: these never reach the permission tool. Claude Code
# matches a ``Bash(prefix:*)`` rule only for a simple command (probed on 2.1.304: ``&&``,
# ``;``, ``|``, ``$(...)``, ``<(...)`` and ``>`` all ask), confines paths to the working dirs
# and applies the ``Read`` deny rules to them. A prefix that has a writing form is either
# exact (``git branch``) or paired with an ``ask`` rule (``ASK_RULES``).
READ_RULES: tuple[str, ...] = (
    "Bash(git log:*)",
    "Bash(git show:*)",
    "Bash(git diff:*)",
    "Bash(git status:*)",
    "Bash(git rev-list:*)",
    "Bash(git blame:*)",
    "Bash(git ls-files:*)",
    "Bash(git branch)",
    "Bash(git branch -a)",
    "Bash(git branch -r)",
    "Bash(git branch -v)",
    "Bash(git branch -vv)",
    "Bash(git branch --show-current)",
    "Bash(git remote -v)",
    "Bash(gh pr view:*)",
    "Bash(gh pr list:*)",
    "Bash(gh pr checks:*)",
    "Bash(gh pr diff:*)",
    "Bash(gh issue view:*)",
    "Bash(gh issue list:*)",
    "Bash(gh run view:*)",
    "Bash(gh run list:*)",
    "Bash(ls:*)",
    "Bash(wc:*)",
    "Bash(cat:*)",
    "Bash(head:*)",
    "Bash(tail:*)",
    "Bash(find:*)",
    "Bash(rg:*)",
    "Bash(grep:*)",
    "Bash(du:*)",
    "Bash(df:*)",
    "Bash(ps)",
    "Bash(ps aux)",
    "Bash(ps -ax)",
    "Bash(ps -A)",
    "Bash(date)",
    "Bash(date -u)",
    "Bash(which:*)",
    "Bash(resonant status)",
)
# Forms of the allowed prefixes that write or run something: always asked (they reach the
# permission tool, which judges them). Probed: an ``ask`` rule beats an ``allow`` rule.
ASK_RULES: tuple[str, ...] = (
    "Bash(git diff *--output*)",
    "Bash(git log *--output*)",
    "Bash(git show *--output*)",
    "Bash(rg *--pre*)",
    "Bash(find *-delete*)",
    "Bash(find *-exec*)",
    "Bash(find *-ok*)",
    "Bash(find *-fprint*)",
    "Bash(find *-fls*)",
)

_OPS = frozenset({"&&", "||", ";", "|", "&", "|&", "\n", ";;", ";&"})
_PUNCT = "();<>|&\n"
_SUBST = ("$(", "`", "<(", ">(", "<<")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_WRAPPERS = frozenset({
    "sudo", "doas", "env", "nohup", "time", "xargs", "exec", "command", "builtin", "nice",
    "timeout", "caffeinate", "watch", "script", "arch", "stdbuf", "chronic", "unbuffer",
})  # fmt: skip
_RUNNERS = {"uv": ("run",), "uvx": (), "npx": (), "pipx": ("run",), "poetry": ("run",)}
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish"})
_KILLERS = frozenset({"kill", "pkill", "killall"})
_LAUNCHCTL_STOP = frozenset({"bootout", "kill", "stop", "remove", "unload", "disable"})

# Reads: the command's first word (and for git and gh, its subcommand).
_PLAIN_READS = frozenset({
    "ls", "wc", "cat", "head", "tail", "find", "rg", "grep", "du", "df", "ps", "date",
    "which", "pwd", "echo", "true",
})  # fmt: skip
_NO_PATHS = frozenset({"ps", "date", "which", "pwd", "echo", "true", "df"})
_GIT_READS = frozenset({
    "log", "show", "diff", "status", "rev-list", "blame", "ls-files", "rev-parse",
    "describe", "shortlog", "branch", "remote", "stash", "tag",
})  # fmt: skip
_GIT_BRANCH_LIST = frozenset({
    "-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose", "--list", "-l",
    "--show-current", "--no-color", "--color", "--merged", "--no-merged", "--contains",
    "--no-contains", "--sort", "--format", "--points-at", "--column", "--no-column",
})  # fmt: skip
_GH_READS = {
    "pr": frozenset({"view", "list", "checks", "diff", "status"}),
    "issue": frozenset({"view", "list", "status"}),
    "run": frozenset({"view", "list"}),
}
_FIND_WRITES = ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fls")
_PUSH_RISKY = frozenset({
    "-f", "--force", "--mirror", "--all", "--tags", "-d", "--delete", "--prune",
    "--force-if-includes", "--atomic",
})  # fmt: skip
_PUSH_RISKY_OPTS = ("--force", "--repo", "--receive-pack", "--exec")
_DEFAULT_BRANCHES = frozenset({"main", "master"})
_CWD_RESET = re.compile(r"Shell cwd was reset to (/[^\n]+)")
_PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/(\d+)")
HOME_WRITE = "writes under ~/.resonant or the keychains aren't allowed"


@dataclass(frozen=True)
class Verdict:
    """What one brain tool call is: ``kind`` (read, write or never), its ``level`` (L0 for a
    read; L3 runs unless tainted; L2 asks), a ``verb`` slug, and ``why`` for a refusal."""

    kind: Kind
    level: Level
    verb: str
    why: str = ""


@dataclass(frozen=True)
class Call:
    """One permission request, judged: the gate's args (``tool``, ``command``, ``cwd``,
    ``shown``, ``verb`` and, when known, ``repo`` and ``digest``) and the verdict."""

    args: dict[str, Any]
    verdict: Verdict

    @property
    def spec(self) -> str:
        return TOOL_READ if self.verdict.kind == "read" else TOOL_WRITE


@dataclass
class _Seg:
    argv: list[str] = field(default_factory=list[str])
    redirects: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    cwd: str = ""


@dataclass(frozen=True)
class _Parsed:
    segs: list[_Seg]
    cwd: str  # after a leading ``cd <dir> &&``
    complex: bool  # substitution, heredoc, subshell or an odd operator
    core: str  # the command after the leading cd (what the args hold)


@dataclass(frozen=True)
class GitFacts:
    """A repo as read from its ``.git``: the work tree's top, the main repo's name, the
    current branch, the default branch (``origin/HEAD``) and whether it's a linked worktree."""

    top: Path
    name: str
    branch: str | None
    default: str | None
    linked: bool


class ShellPolicy:
    """The shell family's rules, bound to this install's paths.

    ``read_ok(path)``: a path the read tools may read (the read roots, off the deny list).
    ``home``: ``RESONANT_HOME`` (writes under it are never allowed) and ``scratch``: the
    brain's own dir under it (``<RESONANT_HOME>/ask``) and the system temp dirs, where a
    write is L3 like a worktree's. ``daemon_pid``: a ``kill`` of it is never allowed."""

    def __init__(
        self,
        *,
        read_ok: Callable[[str], bool],
        home: Path,
        scratch: Sequence[Path],
        user_home: Path | None = None,
        daemon_pid: int | None = None,
        temps: Sequence[Path] | None = None,
    ) -> None:
        self.read_ok = read_ok
        self.home = _real(str(home))
        self.own = tuple(dict.fromkeys(_real(str(p)) for p in scratch))
        tmp = _temp_dirs() if temps is None else temps
        self.scratch = tuple(dict.fromkeys([*self.own, *(_real(str(p)) for p in tmp)]))
        self.user_home = str(user_home or Path.home())
        self.keychains = _real(os.path.join(self.user_home, "Library", "Keychains"))
        self.daemon_pid = daemon_pid if daemon_pid is not None else os.getpid()

    # -- the bridge's entry point --

    def request(self, tool: str, raw: Mapping[str, Any], cwd: str, shown: Callable[[str], str]
                ) -> Call:  # fmt: skip
        """A permission request (Claude Code's tool name and input) as the gate's args and
        its verdict. ``cwd``: the session's shell directory now; ``shown`` redacts a
        command for display (the runner's secrets)."""
        if tool == "Bash":
            command = raw.get("command")
            text = command if isinstance(command, str) else ""
            parsed = self._parse(text, cwd)
            core = parsed.core if parsed is not None else text
            where = parsed.cwd if parsed is not None else cwd
            args: dict[str, Any] = {"tool": tool, "command": core, "cwd": where}
        elif tool in FILE_TOOLS:
            path = raw.get(FILE_TOOLS[tool])
            target = self._abs(path, cwd) if isinstance(path, str) else ""
            digest = hashlib.sha256(json.dumps(dict(raw), sort_keys=True, default=str)
                                    .encode()).hexdigest()[:16]  # fmt: skip
            args = {"tool": tool, "command": target, "cwd": cwd, "digest": digest}
        else:
            args = {"tool": tool, "command": _brief(raw), "cwd": cwd}
        verdict = self.judge(args)
        args["verb"] = verdict.verb
        args["shown"] = _clip(shown(str(args["command"])), SHOWN_CHARS)
        facts = git_facts(str(args["cwd"]) if tool == "Bash" else str(args["command"]))
        if facts is not None:
            args["repo"] = facts.name
        return Call(args, verdict)

    # -- the gate's entry points --

    def judge(self, args: Mapping[str, Any]) -> Verdict:
        """The verdict for the gate's args (``tool``, ``command``, ``cwd``)."""
        tool, command, cwd = args.get("tool"), args.get("command"), args.get("cwd")
        if not isinstance(tool, str) or not isinstance(command, str) or not isinstance(cwd, str):
            return Verdict("never", Level.L2, "unknown", "the request is malformed")
        if tool == "Bash":
            return self._bash(command, cwd)
        if tool in FILE_TOOLS:
            return self._file(tool, command)
        if tool in OWN_READS:
            return Verdict("never", Level.L2, tool.lower(),
                           "outside the folders Claude may read")  # fmt: skip
        if tool in WEB_TOOLS:
            return Verdict("read", Level.L0, tool.lower())
        return Verdict("write", Level.L2, _slug(tool) or "tool")

    def level(self, args: Mapping[str, Any]) -> Level | None:
        """The gate's ``levels`` hook: L3 or L2 for a write (None: keep the spec's)."""
        v = self.judge(args)
        return v.level if v.kind == "write" else None

    def refusal(self, args: Mapping[str, Any]) -> str | None:
        """The gate's ``refusals`` hook: why a call is never allowed, or None."""
        v = self.judge(args)
        return v.why if v.kind == "never" else None

    def summary(self, args: Mapping[str, Any]) -> str:
        """The approval line: what runs and where (redacted, clipped)."""
        tool, shown, cwd = args.get("tool"), args.get("shown"), args.get("cwd")
        what = shown if isinstance(shown, str) else "?"
        where = self._tilde(cwd) if isinstance(cwd, str) else "?"
        if tool == "Bash":
            return f"run `{what}` in {where}"
        if tool in ("Edit", "MultiEdit", "NotebookEdit"):
            return f"edit {self._tilde(what)}"
        if tool == "Write":
            return f"write {self._tilde(what)}"
        return f"use {tool}: {what}"

    # -- after a call ran (the bridge's stream) --

    def cwd_after(self, command: str, cwd: str, output: str) -> str:
        """The shell's directory after ``command`` ran from ``cwd``: Claude Code's Bash keeps
        a ``cd`` (probed: ``cd <dir>`` persists across calls) and says so when it resets it
        (``Shell cwd was reset to <dir>``)."""
        if m := _CWD_RESET.search(output):
            return m.group(1).strip()
        parsed = self._parse(command, cwd)
        if parsed is None:
            return cwd
        segs = parsed.segs
        if len(segs) == 1 and segs[0].argv[:1] == ["cd"] and len(segs[0].argv) == 2:
            return self._abs(segs[0].argv[1], parsed.cwd) or cwd
        return parsed.cwd

    def did(self, args: Mapping[str, Any], output: str) -> str:
        """A change that ran, in a few code-built words for the progress line."""
        tool, verb = args.get("tool"), args.get("verb")
        shown, cwd = str(args.get("shown") or "?"), str(args.get("cwd") or "")
        repo = args.get("repo")
        where = f" in {repo}" if isinstance(repo, str) and repo else ""
        if tool in ("Edit", "MultiEdit", "NotebookEdit"):
            return f"edited {self._tilde(shown)}"
        if tool == "Write":
            return f"wrote {self._tilde(shown)}"
        if verb == "git-push":
            facts = git_facts(cwd)
            branch = f" {facts.branch}" if facts is not None and facts.branch else ""
            return f"pushed{branch}{where}"
        if verb == "gh-pr-create":
            m = _PR_URL.search(output)
            return f"opened PR #{m.group(1)}{where}" if m else f"opened a PR{where}"
        if verb == "git-commit":
            return f"committed{where}"
        return f"ran `{_clip(shown, 60)}`{where}"

    # -- files --

    def _file(self, tool: str, path: str) -> Verdict:
        verb = "write" if tool == "Write" else "edit"
        if not path.startswith("/"):
            return Verdict("write", Level.L2, verb)
        real = _real(path)
        if self._under_home(real):
            return Verdict("never", Level.L2, verb, HOME_WRITE)
        parts = Path(real).parts
        if ".git" in parts or ".claude" in parts:
            return Verdict("write", Level.L2, verb)
        if any(_within(real, s) for s in self.scratch):
            return Verdict("write", Level.L3, verb)
        facts = git_facts(str(Path(real).parent))
        if facts is not None and facts.linked:
            return Verdict("write", Level.L3, verb)
        return Verdict("write", Level.L2, verb)

    # -- bash --

    def _bash(self, command: str, cwd: str) -> Verdict:
        if not command.strip():
            return Verdict("never", Level.L2, "empty", "an empty command")
        parsed = self._parse(command, cwd)
        if parsed is None:  # doesn't tokenize: the never-list on the raw text
            never = self._never_text(command)
            if never is not None:
                return Verdict("never", Level.L2, "never", never)
            return Verdict("write", Level.L2, "unparsed")
        for seg in parsed.segs:
            if (why := self._never_seg(seg.argv, seg.cwd)) is not None:
                return Verdict("never", Level.L2, "never", why)
        writes = [s for s in parsed.segs if not self._read_seg(s)]
        if not writes and not parsed.complex:
            outside = [s for s in parsed.segs if not self._in_scope(s)]
            if outside:
                return Verdict("never", Level.L2, _verb(outside[0].argv),
                               "outside the folders Claude may read")  # fmt: skip
            return Verdict("read", Level.L0, _verb(parsed.segs[0].argv) if parsed.segs else "cd")
        for seg in parsed.segs:
            if self._writes_home(seg):
                return Verdict("never", Level.L2, "never", HOME_WRITE)
        if len(parsed.segs) != 1 or parsed.complex:
            return Verdict("write", Level.L2, "compound")
        seg = parsed.segs[0]
        verb = _verb(seg.argv) if seg.argv else "redirect"
        if any(not _quiet(op, target) for op, target in seg.redirects) or not seg.argv:
            return Verdict("write", Level.L2, verb)
        return Verdict("write", self._write_level(seg), verb)

    def _parse(self, command: str, cwd: str) -> _Parsed | None:
        """The command as segments (None: it doesn't tokenize)."""
        lex = shlex.shlex(command, posix=True, punctuation_chars=_PUNCT)
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        lex.commenters = ""
        try:
            tokens = list(lex)
        except ValueError:
            return None
        complex_ = any(m in command for m in _SUBST)
        segs: list[_Seg] = []
        cur = _Seg()
        i = 0
        while i < len(tokens):
            t = tokens[i]
            if t in _OPS:
                segs.append(cur)
                cur = _Seg()
            elif t in ("(", ")"):
                complex_ = True
            elif t and all(c in "<>&|;" for c in t):
                if not any(c in "<>" for c in t):
                    complex_ = True  # an operator we don't know
                    segs.append(cur)
                    cur = _Seg()
                else:
                    target = tokens[i + 1] if i + 1 < len(tokens) else ""
                    cur.redirects.append((t, target))
                    i += 1
            else:
                cur.argv.append(t)
            i += 1
        segs.append(cur)
        segs = [s for s in segs if s.argv or s.redirects]
        where = cwd
        core = command.strip()
        # a leading ``cd <dir> &&`` (or several) moves the directory for the rest
        while len(segs) > 1 and segs[0].argv[:1] == ["cd"] and len(segs[0].argv) == 2 \
                and not segs[0].redirects:  # fmt: skip
            m = re.match(r"\s*cd\s+(\S+|'[^']*'|\"[^\"]*\")\s*&&\s*", core)
            if m is None:
                break
            target = self._abs(segs[0].argv[1], where)
            if not target:
                break
            where = target
            core = core[m.end():]  # fmt: skip
            segs = segs[1:]
        for s in segs:
            s.cwd = where
            if s.argv[:1] == ["git"]:
                s.cwd = self._git_dir_arg(s.argv, where)
        return _Parsed(segs, where, complex_, core)

    def _git_dir_arg(self, argv: list[str], cwd: str) -> str:
        i = 1
        while i < len(argv) - 1 and argv[i] == "-C":
            cwd = self._abs(argv[i + 1], cwd) or cwd
            i += 2
        return cwd

    def _abs(self, raw: str, cwd: str) -> str:
        """``raw`` as an absolute, normalised path ('' when it holds a variable)."""
        p = self._expand(raw)
        if p is None:
            return ""
        return os.path.normpath(p if p.startswith("/") else os.path.join(cwd, p))

    def _expand(self, raw: str) -> str | None:
        if raw == "~" or raw.startswith("~/"):
            return self.user_home + raw[1:]
        for var in ("$HOME", "${HOME}"):
            if raw == var or raw.startswith(var + "/"):
                return self.user_home + raw[len(var):]  # fmt: skip
        return None if "$" in raw or raw.startswith("~") else raw

    def _tilde(self, path: str) -> str:
        home = self.user_home.rstrip("/") + "/"
        return "~/" + path[len(home):] if path.startswith(home) else path  # fmt: skip

    # -- the never-list --

    @staticmethod
    def _never_text(command: str) -> str | None:
        """The never-list on the raw text, for what doesn't tokenize."""
        if re.search(r"\bresonant\s+secrets?\b", command):
            return "`resonant secret` is only for the owner at the keyboard"
        if re.search(r"\bresonant\s+ext\s+(add|update)\b", command):
            return "`resonant ext add/update` is only for the owner at the keyboard"
        if re.search(r"(^|[\s;&|(`'\"/])security(\s|$)", command):
            return "the login keychain (`security`) is off limits"
        return None

    def _never_seg(self, argv: list[str], cwd: str, depth: int = 0) -> str | None:
        if not argv or depth > 4:
            return None
        for j, tok in enumerate(argv):
            if os.path.basename(tok) == "resonant":
                rest = argv[j + 1 : j + 3]
                if rest[:1] in (["secret"], ["secrets"]):
                    return "`resonant secret` is only for the owner at the keyboard"
                if rest[:1] == ["ext"] and rest[1:2] in (["add"], ["update"]):
                    return "`resonant ext add/update` is only for the owner at the keyboard"
        word = os.path.basename(argv[0])
        if word == "security":
            return "the login keychain (`security`) is off limits"
        if word in _KILLERS:
            for a in argv[1:]:
                if "resonant" in a.lower() or a.lstrip("-") in (str(self.daemon_pid), "1"):
                    return "stopping Resonant's daemon isn't allowed"
        if (
            word == "launchctl"
            and any("com.resonant" in a for a in argv)
            and any(a in _LAUNCHCTL_STOP for a in argv[1:])
        ):
            return "stopping Resonant's daemon isn't allowed"
        if word in _SHELLS and "-c" in argv:
            k = argv.index("-c")
            if k + 1 < len(argv):
                return self._never_nested(argv[k + 1], cwd, depth)
        if word == "eval":
            return self._never_nested(" ".join(argv[1:]), cwd, depth)
        if (
            word in _WRAPPERS
            or word in _RUNNERS
            or _ENV.match(argv[0])
            or word.startswith("python")
        ):  # what it runs starts somewhere after its own options: try each start
            for k in range(1, len(argv)):
                if (why := self._never_seg(argv[k:], cwd, depth + 1)) is not None:
                    return why
        return None

    def _never_nested(self, text: str, cwd: str, depth: int) -> str | None:
        parsed = self._parse(text, cwd)
        if parsed is None:
            return self._never_text(text)
        for seg in parsed.segs:
            if (why := self._never_seg(seg.argv, seg.cwd, depth + 1)) is not None:
                return why
            if self._writes_home(seg):
                return HOME_WRITE
        return None

    def _under_home(self, real: str) -> bool:
        """Never written: ``RESONANT_HOME`` (but the brain's own dir) and the keychains."""
        if _within(real, self.keychains):
            return True
        return _within(real, self.home) and not any(_within(real, s) for s in self.own)

    def _writes_home(self, seg: _Seg) -> bool:
        """A write that touches ``RESONANT_HOME``: run from inside it, or naming a path in
        it (an argument, an ``--opt=path`` value or a redirect target)."""
        if self._under_home(_real(seg.cwd)):
            return True
        names = [*seg.argv[1:], *(t for _, t in seg.redirects)]
        for tok in names:
            for part in (tok, tok.split("=", 1)[-1]):
                if "/" not in part and not part.startswith("~") and not part.startswith("$"):
                    continue
                p = self._abs(part, seg.cwd)
                if p and self._under_home(_real(p)):
                    return True
        return False

    # -- reads --

    def _read_seg(self, seg: _Seg) -> bool:
        """A segment that only reads (paths are checked separately)."""
        argv = seg.argv
        if not argv or _ENV.match(argv[0]):
            return False
        if any(not _quiet(op, t) and op != "<" for op, t in seg.redirects):
            return False
        word = argv[0]
        if word == "cd":
            return len(argv) == 2
        if word == "git":
            return _git_read(argv)
        if word == "gh":
            return len(argv) >= 3 and argv[2] in _GH_READS.get(argv[1], frozenset())
        if os.path.basename(word) == "resonant":
            return argv[1:] == ["status"]
        if word not in _PLAIN_READS:
            return False
        if word == "find":
            return not any(a.startswith(_FIND_WRITES) for a in argv[1:])
        if word == "rg":
            return not any(a.startswith(("--pre", "--hostname-bin")) for a in argv[1:])
        if word == "date":
            return all(a.startswith("+") or a in ("-u", "-R") for a in argv[1:])
        if word == "ps":  # BSD ps: ``e`` / ``-e`` / ``-E`` print every process's environment
            return not any(c in a for a in argv[1:] for c in "eE")
        return True

    def _in_scope(self, seg: _Seg) -> bool:
        """Every path a read names, and the directory it runs in, may be read."""
        argv = seg.argv
        if not argv or argv[0] in _NO_PATHS:
            return True
        if not self._readable(seg.cwd):
            return False
        names = [a for a in argv[1:] if not a.startswith("-")]
        if argv[0] == "cd":
            names = argv[1:]
        names += [t for op, t in seg.redirects if op == "<"]
        for raw in names:
            if raw.startswith("+"):
                continue
            p = self._abs(raw, seg.cwd)
            if not p:
                return False  # a variable: can't tell where it reads
            looks = "/" in raw or raw.startswith(("~", ".")) or os.path.lexists(p)
            if any(c in raw for c in "*?["):
                found = glob.glob(p, include_hidden=True)[: GLOB_MAX + 1]
                if len(found) > GLOB_MAX or not all(self._readable(f) for f in found):
                    return False
                continue
            if looks and not self._readable(p):
                return False
        return True

    def _readable(self, path: str) -> bool:
        real = _real(path)
        if any(_within(real, s) for s in self.own):
            return True  # the brain's own dir
        return self.read_ok(path) and self.read_ok(real)

    # -- write levels --

    def _write_level(self, seg: _Seg) -> Level:
        argv = seg.argv
        if _ENV.match(argv[0]) or argv[0] in _WRAPPERS:
            return Level.L2
        if argv[0] == "git":
            return self._git_level(argv, seg.cwd)
        if argv[0] == "gh" and argv[1:3] == ["pr", "create"]:
            return Level.L3
        return Level.L2

    def _git_level(self, argv: list[str], cwd: str) -> Level:
        sub, rest = _git_sub(argv)
        if sub is None:
            return Level.L2
        flags = [a for a in rest if a.startswith("-")]
        plain = [a for a in rest if not a.startswith("-")]
        if sub in ("add", "commit", "fetch", "switch"):
            return Level.L3
        if sub == "stash" and (not plain or plain[0] in ("push", "save", "list", "show")):
            return Level.L3
        if sub == "checkout" and any(f in ("-b", "-B") for f in flags):
            return Level.L3
        if sub in ("branch", "tag") and len(plain) in (1, 2) and not flags:
            return Level.L3  # a new branch or tag (no -d, -D, -m, -f)
        if sub == "push":
            return self._push_level(rest, cwd)
        return Level.L2

    def _push_level(self, rest: list[str], cwd: str) -> Level:
        if any(a in _PUSH_RISKY or a.startswith(_PUSH_RISKY_OPTS) for a in rest):
            return Level.L2
        plain: list[str] = []
        skip = False
        for a in rest:
            if skip:
                skip = False
                continue
            if a in ("-o", "--push-option"):
                skip = True
                continue
            if not a.startswith("-"):
                plain.append(a)
        facts = git_facts(cwd)
        if facts is None:
            return Level.L2
        refspecs = plain[1:]
        defaults: set[str] = set(_DEFAULT_BRANCHES)
        if facts.default:
            defaults.add(facts.default)
        targets: list[str | None] = []
        if not refspecs:
            targets.append(facts.branch)
        for spec in refspecs:
            if spec.startswith("+") or spec.startswith(":"):
                return Level.L2  # a force or a delete
            dst = spec.split(":", 1)[1] if ":" in spec else spec
            if dst == "HEAD":
                targets.append(facts.branch)
            else:
                targets.append(dst.removeprefix("refs/heads/"))
        if any(t is None or t in defaults or not t for t in targets):
            return Level.L2
        return Level.L3


# --- helpers ---------------------------------------------------------------------------------


def _git_sub(argv: list[str]) -> tuple[str | None, list[str]]:
    """git's subcommand and its args, past ``-C <dir>`` and ``--no-pager``; None when a
    global option could change what runs (``-c``, ``--git-dir``, ``--exec-path``...)."""
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "-C" and i + 1 < len(argv):
            i += 2
            continue
        if a in ("--no-pager", "-P", "--no-optional-locks"):
            i += 1
            continue
        if a.startswith("-"):
            return None, []
        return a, argv[i + 1 :]
    return None, []


def _git_read(argv: list[str]) -> bool:
    sub, rest = _git_sub(argv)
    if sub is None or sub not in _GIT_READS:
        return False
    if any(a.startswith(("--output", "--ext-diff", "--open-files-in-pager")) or a == "-o"
           for a in rest):  # fmt: skip
        return False
    plain = [a for a in rest if not a.startswith("-")]
    if sub == "branch":
        listing = "--list" in rest or "-l" in rest
        return all(a.split("=", 1)[0] in _GIT_BRANCH_LIST for a in rest if a.startswith("-")) \
            and (not plain or listing)  # fmt: skip
    if sub == "remote":
        return rest in ([], ["-v"], ["--verbose"])
    if sub == "stash":
        return plain[:1] in (["list"], ["show"])
    if sub == "tag":
        return not plain or "-l" in rest or "--list" in rest
    return True


def _verb(argv: list[str]) -> str:
    if not argv:
        return "redirect"
    if _ENV.match(argv[0]):
        return "env"
    word = os.path.basename(argv[0])
    if word == "git":
        sub, rest = _git_sub(argv)
        if sub == "push" and any(a in _PUSH_RISKY or a.startswith("--force") for a in rest):
            return "git-push-force"
        return _slug(f"git-{sub}") if sub else "git"
    if word == "gh" and len(argv) >= 3:
        return _slug(f"gh-{argv[1]}-{argv[2]}")
    if word == "resonant" and len(argv) >= 2:
        return _slug(f"resonant-{argv[1]}")
    return _slug(word) or "command"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", text.lower()).strip("-")[:40]


def _quiet(op: str, target: str) -> bool:
    """A redirect that writes nothing: to ``/dev/null`` or between streams."""
    if op in (">&", "<&") and target in ("1", "2", "-"):
        return True
    return op in (">", ">>", "&>", "&>>", ">|") and target == "/dev/null"


def _real(path: str) -> str:
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return path


def _within(path: str, root: str) -> bool:
    root = root.rstrip("/") or "/"
    return path == root or path.startswith(root + "/")


def _temp_dirs() -> list[Path]:
    return [Path(tempfile.gettempdir()), Path("/tmp"), Path("/private/tmp")]  # noqa: S108


def _clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _brief(raw: Mapping[str, Any]) -> str:
    try:
        return _clip(json.dumps(dict(raw), sort_keys=True, default=str), 400)
    except (TypeError, ValueError):
        return "?"


def git_facts(path: str) -> GitFacts | None:
    """The repo ``path`` is in, read from its ``.git`` files (no git process: a repo's
    config could make one run code). None outside a repo."""
    p = Path(path)
    for d in [p, *p.parents]:
        dot = d / ".git"
        try:
            if dot.is_dir():
                return _facts(d, dot, dot, linked=False)
            if dot.is_file():
                text = dot.read_text(errors="replace").strip()
                if not text.startswith("gitdir:"):
                    return None
                gitdir = Path(text.removeprefix("gitdir:").strip())
                if not gitdir.is_absolute():
                    gitdir = (d / gitdir).resolve()
                common = gitdir
                cfile = gitdir / "commondir"
                if cfile.is_file():
                    c = Path(cfile.read_text(errors="replace").strip())
                    common = c if c.is_absolute() else (gitdir / c).resolve()
                return _facts(d, gitdir, common, linked=True)
        except OSError:
            return None
    return None


def _facts(top: Path, gitdir: Path, common: Path, *, linked: bool) -> GitFacts:
    branch = _ref(gitdir / "HEAD", "refs/heads/")
    default = _ref(common / "refs" / "remotes" / "origin" / "HEAD", "refs/remotes/origin/")
    name = common.parent.name if common.name == ".git" else top.name
    return GitFacts(top, name, branch, default, linked)


def _ref(path: Path, prefix: str) -> str | None:
    try:
        text = path.read_text(errors="replace").strip()
    except OSError:
        return None
    if not text.startswith("ref:"):
        return None
    ref = text.removeprefix("ref:").strip()
    return ref.removeprefix(prefix) if ref.startswith(prefix) else None


__all__ = [
    "ASK_RULES",
    "FILE_TOOLS",
    "READ_RULES",
    "TOOL_READ",
    "TOOL_WRITE",
    "Call",
    "GitFacts",
    "ShellPolicy",
    "Verdict",
    "git_facts",
]
