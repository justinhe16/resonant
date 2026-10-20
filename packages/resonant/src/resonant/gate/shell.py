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
    "Bash(git diff *--no-index*)",
    "Bash(git diff *--ext-diff*)",
    "Bash(git log *--ext-diff*)",
    "Bash(git show *--ext-diff*)",
    "Bash(rg *--hostname-bin*)",
)

_OPS = frozenset({"&&", "||", ";", "|", "&", "|&", "\n", ";;", ";&"})
_PUNCT = "();<>|&\n"
_SUBST = ("$(", "`", "<(", ">(", "<<")
# Anything the shell expands or that quoting could hide (braces, variables, escapes, ``~``
# after ``=`` or ``:``): never a read and never L3, so what's judged is what runs.
_EXPANDS = ("{", "}", "$", "`", "\\", "=~", ":~")
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
_DEFAULT_BRANCHES = frozenset({"main", "master", "develop", "trunk", "production", "prod"})
_NAME = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._/-]*")  # a plain branch, tag or remote name
# Flags an L3 git command may carry (anything else asks): the value-taking ones are listed
# in ``_GIT_VALUE``.
_GIT_L3_FLAGS: Mapping[str, frozenset[str]] = {
    "add": frozenset({"-A", "--all", "-u", "--update", "-N", "--intent-to-add", "-v", "--"}),
    "commit": frozenset({"-m", "--message", "-a", "--all", "-q", "--quiet", "--no-edit",
                         "--amend", "-s", "--signoff", "--allow-empty", "-v"}),
    "fetch": frozenset({"-q", "--quiet", "--prune", "-p", "--all", "--tags", "--no-tags"}),
    "switch": frozenset({"-c", "--create", "-q", "--quiet"}),
    "checkout": frozenset({"-b", "-q", "--quiet"}),
    "stash": frozenset({"-m", "--message", "-u", "--include-untracked", "-q", "--quiet"}),
    "branch": frozenset(),
    "tag": frozenset(),
    "push": frozenset({"-u", "--set-upstream", "-q", "--quiet", "-v", "--verbose",
                       "--no-verify", "--dry-run", "-n"}),
}  # fmt: skip
_GIT_VALUE = frozenset({"-m", "--message"})
_GH_RISKY = frozenset({"-F", "--body-file", "-T", "--template", "--recover"})
_HOOK_FILES = frozenset({
    ".husky", ".githooks", "hooks", ".pre-commit-config.yaml", "lefthook.yml", "lefthook.yaml",
    ".lefthook.yml", "Makefile", "package.json", ".envrc", ".gitattributes", ".gitmodules",
    "setup.py", "pyproject.toml", "conftest.py", ".npmrc",
})  # fmt: skip
_SCRIPT_EXT = (".sh", ".bash", ".zsh", ".py", ".js", ".mjs", ".ts", ".rb", ".pl", ".command")
_FIND_VALUES = frozenset({"-name", "-iname", "-path", "-ipath", "-regex", "-iregex",
                          "-wholename", "-type", "-maxdepth", "-mindepth", "-size", "-mtime",
                          "-newer", "-user", "-group", "-perm"})  # fmt: skip
_GREP_VALUES = frozenset({"-e", "--regexp", "-g", "--glob", "-t", "--type", "-m", "-A", "-B",
                          "-C"})  # fmt: skip
_REMOTE = re.compile(r'^\s*\[remote "([^"]+)"\]', re.M)
UNKNOWN_CWD = "?"  # the shell's directory after a cd we couldn't follow
_HOMEISH = re.compile(r"(^|/)(\.resonant|Keychains)(/|$)")
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
    after: str = ""  # the operator after it


@dataclass(frozen=True)
class _Parsed:
    segs: list[_Seg]
    cwd: str  # after a leading ``cd <dir> &&``
    complex: bool  # substitution, expansion, heredoc, subshell, a later cd, an odd operator


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
            # The args hold the whole command as Claude Code will run it: what's judged and
            # approved is exactly what runs. ``dir`` is where it runs (after a leading cd).
            args: dict[str, Any] = {"tool": tool, "command": text, "cwd": cwd}
            parsed = self._parse(text, cwd) if cwd.startswith("/") else None
            if parsed is not None and parsed.cwd != cwd:
                args["dir"] = parsed.cwd
        elif tool in FILE_TOOLS:
            path = raw.get(FILE_TOOLS[tool])
            target = self._abs(path, cwd) if isinstance(path, str) else ""
            digest = hashlib.sha256(json.dumps(dict(raw), sort_keys=True, default=str)
                                    .encode()).hexdigest()[:16]  # fmt: skip
            args = {"tool": tool, "command": target, "cwd": cwd, "digest": digest}
            content = raw.get("content")
            if tool == "Write" and isinstance(content, str) and content.startswith("#!"):
                args["script"] = "yes"
        else:
            args = {"tool": tool, "command": _brief(raw), "cwd": cwd}
        verdict = self.judge(args)
        args["verb"] = verdict.verb
        args["shown"] = _clip(shown(str(args["command"])), SHOWN_CHARS)
        where = str(args.get("dir") or args["cwd"])
        facts = git_facts(where if tool == "Bash" else str(args["command"]))
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
            return self._file(tool, command, script=args.get("script") == "yes")
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
        tool, shown = args.get("tool"), args.get("shown")
        cwd = args.get("dir") or args.get("cwd")
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
        parsed = self._parse(command, cwd) if cwd.startswith("/") else None
        if parsed is None:
            return cwd
        segs = parsed.segs
        if len(segs) == 1 and segs[0].argv[:1] == ["cd"] and len(segs[0].argv) == 2:
            return self._abs(segs[0].argv[1], parsed.cwd) or UNKNOWN_CWD
        if any(s.argv[:1] in (["cd"], ["pushd"], ["popd"]) for s in segs):
            return UNKNOWN_CWD  # a cd we can't follow: nothing relative is trusted after it
        return parsed.cwd

    def did(self, args: Mapping[str, Any], output: str) -> str:
        """A change that ran, in a few code-built words for the progress line."""
        tool, verb = args.get("tool"), args.get("verb")
        shown, cwd = str(args.get("shown") or "?"), str(args.get("dir") or args.get("cwd") or "")
        repo = args.get("repo")
        where = f" in {repo}" if isinstance(repo, str) and repo else ""
        if tool in ("Edit", "MultiEdit", "NotebookEdit"):
            return f"edited {self._tilde(shown)}"
        if tool == "Write":
            return f"wrote {self._tilde(shown)}"
        if verb in ("git-push", "git-push-default"):
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

    def _file(self, tool: str, path: str, *, script: bool = False) -> Verdict:
        verb = "write" if tool == "Write" else "edit"
        if not path.startswith("/"):
            return Verdict("write", Level.L2, verb)
        real = _real(path)
        if self._under_home(real):
            return Verdict("never", Level.L2, verb, HOME_WRITE)
        parts = Path(real).parts
        if ".git" in parts or ".claude" in parts:
            return Verdict("write", Level.L2, verb)
        if any(p in _HOOK_FILES for p in parts):
            return Verdict("write", Level.L2, verb)  # code a later git commit or build runs
        if any(_within(real, s) for s in self.scratch):
            if script or real.endswith(_SCRIPT_EXT):
                return Verdict("write", Level.L2, verb)  # a script to run next
            return Verdict("write", Level.L3, verb)
        facts = git_facts(str(Path(real).parent))
        if facts is not None and facts.linked:
            return Verdict("write", Level.L3, verb)
        return Verdict("write", Level.L2, verb)

    # -- bash --

    def _bash(self, command: str, cwd: str) -> Verdict:
        if not command.strip():
            return Verdict("never", Level.L2, "empty", "an empty command")
        known = cwd.startswith("/")
        parsed = self._parse(command, cwd if known else "/")
        if parsed is None:  # doesn't tokenize: the never-list on the raw text
            never = self._never_text(command)
            if never is not None:
                return Verdict("never", Level.L2, "never", never)
            return Verdict("write", Level.L2, "unparsed")
        for seg in parsed.segs:
            if (why := self._never_seg(seg.argv, seg.cwd)) is not None:
                return Verdict("never", Level.L2, "never", why)
        complex_ = parsed.complex or not known
        writes = [s for s in parsed.segs if not self._read_seg(s)]
        if not writes and not complex_:
            scopes = [self._in_scope(s) for s in parsed.segs]
            if "outside" in scopes:
                seg = parsed.segs[scopes.index("outside")]
                return Verdict("never", Level.L2, _verb(seg.argv),
                               "outside the folders Claude may read")  # fmt: skip
            if "unsure" not in scopes:
                return Verdict("read", Level.L0,
                               _verb(parsed.segs[0].argv) if parsed.segs else "cd")  # fmt: skip
        for seg in parsed.segs:
            if self._writes_home(seg, known=known and not parsed.complex):
                return Verdict("never", Level.L2, "never", HOME_WRITE)
        if len(parsed.segs) != 1 or complex_:
            return Verdict("write", Level.L2, "compound")
        seg = parsed.segs[0]
        verb = _verb(seg.argv) if seg.argv else "redirect"
        if any(not _quiet(op, target) for op, target in seg.redirects) or not seg.argv:
            return Verdict("write", Level.L2, verb)
        if verb == "git-push":
            kind = self._push_kind(seg)
            if kind != "feature":
                return Verdict("write", Level.L2, f"git-push-{kind}")
            return Verdict("write", Level.L3, verb)
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
        complex_ = any(m in command for m in (*_SUBST, *_EXPANDS))
        segs: list[_Seg] = []
        cur = _Seg()
        i = 0
        while i < len(tokens):
            t = tokens[i]
            if t in _OPS:
                cur.after = t
                segs.append(cur)
                cur = _Seg()
            elif t in ("(", ")"):
                complex_ = True
            elif t and all(c in "<>&|;" for c in t):
                if not any(c in "<>" for c in t):
                    complex_ = True  # an operator we don't know
                    cur.after = t
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
        # a leading ``cd <dir> &&`` (or several) moves the directory for the rest
        while (len(segs) > 1 and segs[0].argv[:1] == ["cd"] and len(segs[0].argv) == 2
               and not segs[0].redirects and segs[0].after == "&&"):  # fmt: skip
            target = self._abs(segs[0].argv[1], where)
            if not target:
                break
            where = target
            segs = segs[1:]
        if len(segs) > 1 and any(s.argv[:1] in (["cd"], ["pushd"], ["popd"]) for s in segs):
            complex_ = True  # a directory change we don't follow
        for s in segs:
            s.cwd = where
            if s.argv[:1] == ["git"]:
                s.cwd = self._git_dir_arg(s.argv, where)
        return _Parsed(segs, where, complex_)

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
                if rest[:1] in (["approvals"], ["approve"], ["deny"]):
                    return "Claude can't answer the owner's approvals"
        word = os.path.basename(argv[0])
        if word == "security":
            return "the login keychain (`security`) is off limits"
        if word in _KILLERS and self._kills_daemon(word, argv[1:]):
            return "stopping Resonant's daemon isn't allowed"
        if (
            word == "launchctl"
            and any("com.resonant" in a for a in argv)
            and any(a in _LAUNCHCTL_STOP for a in argv[1:])
        ):
            return "stopping Resonant's daemon isn't allowed"
        if word in _SHELLS:
            for k, a in enumerate(argv[1:], 1):  # ``-c``, ``-lc``, ``-ic`` ...
                if a.startswith("-") and not a.startswith("--") and "c" in a and k + 1 < len(argv):
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

    def _kills_daemon(self, word: str, args: list[str]) -> bool:
        """``kill`` of the daemon's pid or of every process (``-1``); ``pkill``/``killall``
        of anything that could match the daemon (``resonant``, ``python``, ``uv``)."""
        if word == "kill":
            targets = [a for a in args if not a.startswith("-") or a in ("-1", "--")]
            if args[-1:] == ["-1"] or ("--" in args and args[args.index("--") + 1 :] == ["-1"]):
                return True
            return any(t.lstrip("-") == str(self.daemon_pid) for t in targets)
        text = " ".join(args).lower()
        return any(name in text for name in ("resonant", "python", "uv"))

    def _never_nested(self, text: str, cwd: str, depth: int) -> str | None:
        parsed = self._parse(text, cwd)
        if parsed is None:
            return self._never_text(text)
        for seg in parsed.segs:
            if (why := self._never_seg(seg.argv, seg.cwd, depth + 1)) is not None:
                return why
            if self._writes_home(seg, known=False):
                return HOME_WRITE
        return None

    def _under_home(self, real: str) -> bool:
        """Never written: ``RESONANT_HOME`` (but the brain's own dir) and the keychains."""
        if _within(real, self.keychains):
            return True
        return _within(real, self.home) and not any(_within(real, s) for s in self.own)

    def _writes_home(self, seg: _Seg, *, known: bool = True) -> bool:
        """A write that touches ``RESONANT_HOME`` or the keychains: run from inside it, or
        naming a path in it (an argument, an ``--opt=path`` value or a redirect target).
        Every name is resolved from the directory; one that can't be (a variable, an
        unknown directory) is never allowed to name ``.resonant`` or ``Keychains``."""
        if known and self._under_home(_real(seg.cwd)):
            return True
        names = [*seg.argv, *(t for _, t in seg.redirects)]  # the command word too (``a${IFS}b``)
        for tok in names:
            for part in dict.fromkeys((tok, tok.split("=", 1)[-1])):
                if _HOMEISH.search(part) and not known:
                    return True
                p = self._abs(part, seg.cwd) if known else ""
                if p:
                    hits = glob.glob(p)[:GLOB_MAX] if any(c in part for c in "*?[") else [p]
                    if any(self._under_home(_real(h)) for h in [p, *hits]):
                        return True
                elif _HOMEISH.search(part):
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

    def _in_scope(self, seg: _Seg) -> Literal["ok", "outside", "unsure"]:
        """Every path a read names, and the directory it runs in, may be read: ``ok``, or
        ``outside`` (refused, as the read tools do), or ``unsure`` (asked)."""
        argv = seg.argv
        if not argv or argv[0] in _NO_PATHS:
            return "ok"
        if not self._readable(seg.cwd):
            return "outside"
        names = _path_args(argv)
        names += [t for op, t in seg.redirects if op == "<"]
        for raw in names:
            if raw.startswith("+"):
                continue
            p = self._abs(raw, seg.cwd)
            if not p:
                return "unsure"
            looks = "/" in raw or raw.startswith(("~", ".")) or os.path.lexists(p)
            if any(c in raw for c in "*?["):
                found = glob.glob(p, include_hidden=True)[: GLOB_MAX + 1]
                if len(found) > GLOB_MAX:
                    return "unsure"
                if not all(self._readable(f) for f in found):
                    return "outside"
                continue
            if looks and not self._readable(p):
                return "outside"
        return "ok"

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
            risky = any(a.split("=", 1)[0] in _GH_RISKY for a in argv[3:])
            return Level.L2 if risky else Level.L3
        return Level.L2

    def _git_level(self, argv: list[str], cwd: str) -> Level:
        """L3 only for local branch work with known flags and plain names; else L2."""
        sub, rest = _git_sub(argv)
        if sub is None or sub == "push" or sub not in _GIT_L3_FLAGS:
            return Level.L2
        flags, plain = _flags(rest)
        if flags is None or not flags <= _GIT_L3_FLAGS[sub]:
            return Level.L2
        if sub in ("add", "commit"):
            return Level.L3 if all(not p.startswith("/") for p in plain) else Level.L2
        if not all(_NAME.fullmatch(p) for p in plain):
            return Level.L2
        if sub == "fetch":
            remotes = git_remotes(cwd)
            return Level.L3 if not plain or (plain[0] in remotes and len(plain) == 1) else Level.L2
        if sub == "switch":
            return Level.L3 if len(plain) == 1 else Level.L2
        if sub == "checkout":
            return Level.L3 if "-b" in flags and len(plain) in (1, 2) else Level.L2
        if sub == "stash":
            return Level.L3 if not plain or plain[0] in ("push", "save", "list", "show") else (
                Level.L2)  # fmt: skip
        return Level.L3 if len(plain) in (1, 2) else Level.L2  # a new branch or tag

    def _push_kind(self, seg: _Seg) -> Literal["feature", "default", "force"]:
        """A push: ``feature`` (L3: a plain non-default branch to a configured remote, known
        flags only), ``default`` (to a default branch, or one we can't tell) or ``force``
        (force, delete, mirror, odd flags or refspecs)."""
        _, rest = _git_sub(seg.argv)
        flags, plain = _flags(rest)
        if flags is None or not flags <= _GIT_L3_FLAGS["push"]:
            return "force"
        facts = git_facts(seg.cwd)
        if facts is None:
            return "default"
        remotes = git_remotes(seg.cwd)
        if plain and plain[0] not in remotes:
            return "force"  # a URL or path, not a configured remote
        defaults: set[str] = set(_DEFAULT_BRANCHES)
        if facts.default:
            defaults.add(facts.default)
        targets: list[str | None] = []
        refspecs = plain[1:]
        if not refspecs:
            upstream = git_upstream(seg.cwd, facts.branch)
            targets.append(facts.branch if upstream in (None, facts.branch) else None)
        for spec in refspecs:
            src, _, dst = spec.partition(":")
            dst = dst or src
            if not src or src.startswith("+") or spec == "tag":
                return "force"  # a delete, a force or a tag push
            if not (src == "HEAD" or _NAME.fullmatch(src)) or not _NAME.fullmatch(dst):
                return "force"
            if dst == "HEAD":
                targets.append(facts.branch)
                continue
            for prefix in ("refs/heads/", "heads/"):
                dst = dst.removeprefix(prefix)
            targets.append(None if dst.startswith("refs/") else dst)
        for t in targets:
            if t is None or t in defaults or t.startswith("release"):
                return "default"
        return "feature"


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


def _flags(rest: list[str]) -> tuple[frozenset[str] | None, list[str]]:
    """``rest`` as its flags (combined short ones split: ``-fu`` is ``-f -u``; a
    ``--name=value`` by its name) and its plain words, past a ``-m``'s value. None when a
    flag can't be read."""
    flags: set[str] = set()
    plain: list[str] = []
    skip = False
    for a in rest:
        if skip:
            skip = False
            continue
        if a == "--" or not a.startswith("-"):
            plain.append(a)
            continue
        if a.startswith("--"):
            name = a.split("=", 1)[0]
            flags.add(name)
            skip = name in _GIT_VALUE and "=" not in a
            continue
        letters = a[1:]
        if not letters.isalpha():
            return None, plain
        for k, c in enumerate(letters):
            flags.add(f"-{c}")
            if f"-{c}" in _GIT_VALUE:
                skip = k == len(letters) - 1  # ``-mfix``: the rest is the value
                break
    return frozenset(flags), plain


def _path_args(argv: list[str]) -> list[str]:
    """The words of a read that may name a path: not flags, not ``find``'s patterns, not
    ``grep``'s or ``rg``'s pattern."""
    out: list[str] = []
    skip = False
    pattern_done = argv[0] not in ("grep", "rg") or any(
        a in ("-e", "--regexp", "-f", "--file") or a.startswith("--regexp=") for a in argv
    )
    for a in argv[1:]:
        if skip:
            skip = False
            continue
        if a.startswith("-"):
            grep = argv[0] in ("grep", "rg") and a in _GREP_VALUES
            skip = (argv[0] == "find" and a in _FIND_VALUES) or grep
            continue
        if argv[0] == "cd" or pattern_done:
            out.append(a)
        else:
            pattern_done = True
    return out


def git_remotes(path: str) -> frozenset[str]:
    """The remotes configured for the repo at ``path`` (from its config file)."""
    return frozenset(m.group(1) for m in _REMOTE.finditer(_git_config(path)))


def git_upstream(path: str, branch: str | None) -> str | None:
    """The branch ``branch`` pushes to when its upstream says so (``branch.<b>.merge``)."""
    if branch is None:
        return None
    text = _git_config(path)
    m = re.search(rf'\[branch "{re.escape(branch)}"\]([^\[]*)', text)
    merge = re.search(r"^\s*merge\s*=\s*(\S+)", m.group(1), re.M) if m else None
    return merge.group(1).removeprefix("refs/heads/") if merge else None


def _git_config(path: str) -> str:
    p = Path(path)
    for d in [p, *p.parents]:
        dot = d / ".git"
        try:
            if dot.is_dir():
                return (dot / "config").read_text(errors="replace")
            if dot.is_file():
                text = dot.read_text(errors="replace").strip().removeprefix("gitdir:").strip()
                gitdir = Path(text) if Path(text).is_absolute() else (d / text).resolve()
                common = gitdir
                cfile = gitdir / "commondir"
                if cfile.is_file():
                    c = Path(cfile.read_text(errors="replace").strip())
                    common = c if c.is_absolute() else (gitdir / c).resolve()
                return (common / "config").read_text(errors="replace")
        except OSError:
            return ""
    return ""


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
