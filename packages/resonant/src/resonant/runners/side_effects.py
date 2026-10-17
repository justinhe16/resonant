"""Side effects read from a worker's own commands (TR-1).

GT-1 asks workers to log what they do outside their branch and PR with ``resonant note
"did: …"``. That depends on the worker remembering. This module reads the worker's tool calls
in its stream-json output and names the outside actions in them, so Resonant can list them
itself (``reports.record_detected``, marked ``(detected)`` on the run's ``did:`` line) and text
the high-impact ones right away.

Pure and deterministic: command text in, :class:`SideEffect` out. It never runs anything and
never claims more than the text says: ``git push origin main`` is "pushed to main", not "main
changed" (the push may have failed). Every ``what`` is code-built from a few parsed tokens,
each kept to ``[A-Za-z0-9._/@:+#-]`` and clipped, so no request body, header, token or
free text of the command reaches a report. For HTTP calls only the method and the host are
kept.

What counts (``classify_command``):

- ``git push``: to the base branch (high; ``main``/``master`` when the base isn't known), a
  branch delete (high), ``--all``/``--mirror`` (high), tags, another branch, a remote other
  than ``origin`` or a URL of a repo other than the run's. A push of the run's own branch (no
  refspec, ``HEAD``, or ``resonant/r<n>-…``) is the run's work, not a side effect.
- ``gh``: ``pr merge``, ``release create|upload|edit|delete``, ``repo create|delete|archive|
  rename|edit``, ``workflow run``, ``secret set|delete``, ``issue create|comment|close``,
  ``pr comment|close|review``, ``api`` with a writing method (or fields, which make it POST).
- Deploys: ``fly``/``flyctl deploy``, ``vercel`` (``--prod``), ``netlify deploy``, ``kubectl
  apply|create|delete|replace|patch|scale|rollout``, ``helm install|upgrade|uninstall``,
  ``terraform``/``tofu apply|destroy``, ``pulumi up|destroy``, ``docker push`` (and ``buildx
  … --push``), ``wrangler deploy|publish``, ``firebase deploy``, ``railway up``, ``heroku``
  releases, ``serverless``/``sls deploy``, ``cdk deploy|destroy``, ``gcloud … deploy``,
  ``aws s3 cp|sync|rm|mv`` and ``aws … deploy``.
- Publishing (high): ``npm``/``pnpm``/``yarn``/``bun publish``, ``uv publish``, ``twine
  upload``, ``poetry publish``, ``cargo publish``, ``gem push``, ``hatch``/``flit publish``.
- HTTP writes: ``curl`` (``-X``/``--request`` or data flags), ``http``/``https``/``xh``
  (httpie: a method word, or ``k=v`` items), ``wget`` (``--method``/``--post-data``). DELETE is
  high.
- ``resonant restart``/``update`` (the worker CLI; Resonant also records the ones that went
  through).
- Email and messages: ``mail``, ``mailx``, ``sendmail``, ``mutt``, ``neomutt``, ``msmtp``,
  ``himalaya … send``, ``osascript`` scripting Messages or Mail, ``slack … send``,
  ``twilio … messages``.

``classify_tool`` adds MCP tool calls whose name says they write (``mcp__slack__send_message``,
``mcp__linear__delete_issue``: delete and remove are high). Wrappers are looked through
(``sudo``, ``env``, ``time``, ``nohup``, ``npx``, ``uvx``, ``uv run``, ``bash -c '…'``), and a
compound line (``&&``, ``;``, ``|``, newlines) is read command by command.

``keys`` let a report drop a detected effect the worker (or Resonant) already listed: it is a
duplicate when every key appears in one ``did:`` item, ignoring case (``merge`` and ``12``
match "merged PR #12 with gh").
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast
from urllib.parse import urlsplit

Kind = Literal[
    "push",
    "merge",
    "release",
    "repo",
    "deploy",
    "publish",
    "http",
    "self",
    "message",
    "mcp",
    "github",
]

TOKEN_MAX = 40
WHAT_MAX = 80
_MAX_DEPTH = 2  # nested ``bash -c`` levels read
_SAFE = re.compile(r"[^A-Za-z0-9._/@:+#-]")
_OPS = frozenset({"&&", "||", ";", "|", "&", "|&", ";;", "(", ")", "{", "}", "!"})
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
_OWN_BRANCH = re.compile(r"resonant/r[0-9]+(?:-.*)?", re.IGNORECASE)
_GITHUB = re.compile(
    r"(?:https?://(?:[^@/]+@)?github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"(?P<slug>[A-Za-z0-9._-]+/[A-Za-z0-9._-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1"})  # noqa: S104
_MCP_HIGH = re.compile(r"delete|remove|pay|charge|transfer|refund", re.IGNORECASE)
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_MCP_WRITE = re.compile(
    r"(?:^|_)(send|post|create|delete|remove|update|merge|publish|deploy|write|reply|comment|"
    r"close|archive|transfer|pay|charge|invite|upload|push|cancel|refund)(?:_|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SideEffect:
    """One outside action seen in a command. ``what`` is code-built and short; ``high`` marks
    the ones worth texting right away (base push, publish, delete)."""

    kind: Kind
    what: str
    high: bool = False
    keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunContext:
    """What tells the run's own work apart: ``repo`` (``owner/name`` or a short name), the
    base branch and the run's branch."""

    repo: str | None = None
    base: str | None = None
    branch: str | None = None


# --- tokens ------------------------------------------------------------------------------


def tok(text: str, limit: int = TOKEN_MAX) -> str:
    """A token safe to show: unsafe characters dropped, clipped."""
    flat = _SAFE.sub("", text)
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def host_of(url: str) -> str | None:
    """The host of a URL (no user, password, port, path or query), or None."""
    text = url if "://" in url else f"https://{url}"
    try:
        host = urlsplit(text).hostname
    except ValueError:
        return None
    if not host or not re.fullmatch(r"[a-z0-9.-]+", host):
        return None
    return tok(host, 60)


def _words(cmd: str) -> list[str]:
    try:
        lex = shlex.shlex(cmd.replace("\n", " ; "), posix=True, punctuation_chars=";&|()")
        lex.whitespace_split = True
        lex.commenters = ""
        return list(lex)
    except ValueError:  # unbalanced quotes: read it roughly
        rough = re.split(r"\s+", cmd.replace("\n", " ; ").strip())
        return [w.strip("'\"") for w in rough if w.strip("'\"")]


def commands(cmd: str) -> list[list[str]]:
    """``cmd`` split into simple commands (argv lists), operators dropped."""
    out: list[list[str]] = []
    cur: list[str] = []
    for w in _words(cmd):
        if w in _OPS or (w and set(w) <= set(";&|")):
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(w)
    if cur:
        out.append(cur)
    return out


def _base(word: str) -> str:
    return word.rsplit("/", 1)[-1].lower()


_WRAPPERS = frozenset(
    {"sudo", "time", "nohup", "command", "exec", "nice", "caffeinate", "doppler", "dotenv", "op"}
)
_RUNNERS = {"npx": 1, "bunx": 1, "uvx": 1, "pipx": 2}  # argv0 -> words to drop (pipx run)


def unwrap(argv: Sequence[str]) -> list[str]:
    """``argv`` with leading assignments and wrappers removed (``sudo -E env A=1 npx x``)."""
    a = list(argv)
    for _ in range(8):
        while a and _ASSIGN.fullmatch(a[0]):
            a = a[1:]
        if not a:
            return a
        head = _base(a[0])
        if head in _WRAPPERS or head == "env":
            a = a[1:]
            while a and (a[0].startswith("-") or _ASSIGN.fullmatch(a[0])):
                a = a[1:]
            if head in ("doppler", "op", "dotenv") and "--" in a:  # doppler run -- cmd
                a = a[a.index("--") + 1 :]
            elif head in ("doppler", "op") and a and a[0] == "run":
                a = a[1:]
            continue
        if head in _RUNNERS:
            a = a[_RUNNERS[head] :]
            while a and a[0].startswith("-"):
                a = a[1:]
            continue
        if head in ("uv", "poetry", "pdm", "hatch") and len(a) > 1 and a[1] == "run":
            a = a[2:]
            while a and a[0].startswith("-"):
                a = a[1:]
            continue
        if head in ("pnpm", "yarn", "bun") and len(a) > 1 and a[1] in ("exec", "dlx", "x"):
            a = a[2:]
            continue
        return a
    return a


def _positional(args: Sequence[str], with_value: Iterable[str] = ()) -> list[str]:
    """``args`` minus options; options in ``with_value`` take the next word too."""
    takes = set(with_value)
    out: list[str] = []
    skip = False
    for x in args:
        if skip:
            skip = False
            continue
        if x == "--":
            continue
        if x.startswith("-"):
            if x in takes:
                skip = True
            continue
        out.append(x)
    return out


def _opt(args: Sequence[str], *names: str) -> str | None:
    """The value of the first option in ``names`` (``-X POST``, ``-XPOST``, ``--x=POST``)."""
    for i, x in enumerate(args):
        for n in names:
            if x == n and i + 1 < len(args):
                return args[i + 1]
            if n.startswith("--") and x.startswith(n + "="):
                return x[len(n) + 1 :]
            if len(n) == 2 and not n.startswith("--") and x.startswith(n) and len(x) > 2:
                return x[2:]
    return None


def _has(args: Sequence[str], *names: str) -> bool:
    return any(
        x in names or any(x.startswith(n + "=") for n in names if n.startswith("--")) for x in args
    )


# --- git -----------------------------------------------------------------------------------

_GIT_GLOBAL_VALUE = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
)
_PUSH_VALUE = ("-o", "--push-option", "--receive-pack", "--exec", "--repo")


def _same_repo(slug: str, repo: str | None) -> bool:
    if not repo:
        return True  # unknown: don't call it another repo
    want = repo.lower().removesuffix(".git")
    have = slug.lower()
    return have == want or (have.rsplit("/", 1)[-1] == want.rsplit("/", 1)[-1] and "/" not in want)


def _git(args: list[str], ctx: RunContext) -> list[SideEffect]:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in _GIT_GLOBAL_VALUE else 1
    if i >= len(args) or args[i] != "push":
        return []
    return _push(args[i + 1 :], ctx)


def _push(args: list[str], ctx: RunContext) -> list[SideEffect]:
    pos = _positional(args, _PUSH_VALUE)
    force = _has(args, "-f", "--force", "--force-with-lease", "--force-if-includes")
    delete = _has(args, "-d", "--delete")
    remote = pos[0] if pos else (_opt(args, "--repo") or "origin")
    specs = pos[1:]
    m = _GITHUB.search(remote)
    other: str | None = None
    if m is not None and not _same_repo(m["slug"], ctx.repo):
        other = tok(m["slug"])
    elif m is None and remote != "origin":
        shown = host_of(remote) if "://" in remote or "@" in remote else tok(remote)
        other = f"remote {shown or '?'}"
    where = f" to {other}" if other else ""
    bases = {ctx.base} if ctx.base else {"main", "master"}
    out: list[SideEffect] = []
    verb = "force-pushed" if force else "pushed"
    if _has(args, "--mirror"):
        return [SideEffect("push", f"pushed --mirror{where}", True, ("mirror",))]
    if _has(args, "--all", "--branches"):
        out.append(SideEffect("push", f"{verb} all branches{where}", True, ("push", "all")))
    if _has(args, "--tags"):
        out.append(SideEffect("push", f"pushed tags{where}", False, ("tag",)))
    if not specs:
        if other and not out:
            out.append(SideEffect("push", f"{verb}{where}", False, ("push",)))
        return out
    for spec in specs:
        plus = spec.startswith("+")
        src, _, dst = spec.lstrip("+").partition(":")
        dst = dst or src
        name = re.sub(r"^refs/(heads/)?", "", dst)
        gone = delete or (spec.lstrip("+").startswith(":") and bool(dst))
        v = "force-pushed" if (force or plus) else "pushed"
        if dst.startswith("refs/tags/") or src.startswith("refs/tags/"):
            out.append(
                SideEffect(
                    "push", f"pushed tag {tok(name.removeprefix('tags/'))}{where}", False, ("tag",)
                )
            )
        elif gone:
            out.append(
                SideEffect(
                    "push", f"deleted branch {tok(name)}{where}", True, ("delet", tok(name).lower())
                )
            )
        elif name in bases:
            out.append(
                SideEffect("push", f"{v} to {tok(name)}{where}", True, ("push", tok(name).lower()))
            )
        elif name in ("HEAD", "@") or name == ctx.branch or _OWN_BRANCH.fullmatch(name):
            if other:
                out.append(SideEffect("push", f"{v}{where}", False, ("push",)))
        else:
            out.append(
                SideEffect(
                    "push", f"{v} branch {tok(name)}{where}", False, ("push", tok(name).lower())
                )
            )
    return out


# --- gh ------------------------------------------------------------------------------------

_GH_VALUE = (
    "-R",
    "--repo",
    "-t",
    "--title",
    "-b",
    "--body",
    "-F",
    "--body-file",
    "-n",
    "--notes",
    "--notes-file",
    "--target",
    "-m",
    "--message",
    "--subject",
    "-X",
    "--method",
    "-f",
    "--field",
    "--raw-field",
    "-H",
    "--header",
    "--input",
    "-q",
    "--jq",
    "--ref",
    "--visibility",
    "--description",
    "-r",
    "--reviewer",
    "-l",
    "--label",
    "-a",
    "--assignee",
    "--env",
    "-e",
    "--org",
    "-o",
    "--app",
)


def _in_repo(args: Sequence[str]) -> str:
    r = _opt(args, "-R", "--repo")
    return f" in {tok(r)}" if r else ""


def _gh(args: list[str]) -> list[SideEffect]:
    pos = _positional(args, _GH_VALUE)
    if not pos:
        return []
    group, sub = pos[0], (pos[1] if len(pos) > 1 else "")
    rest = pos[2:]
    where = _in_repo(args)
    if group == "pr" and sub == "merge":
        n = _pr_number(rest)
        label = f"PR #{n}" if n else "a PR"
        return [
            SideEffect(
                "merge", f"merged {label} with gh{where}", False, ("merge", *((n,) if n else ()))
            )
        ]
    if group == "pr" and sub in ("close", "comment", "review"):
        n = _pr_number(rest)
        label = f"PR #{n}" if n else "a PR"
        verb = {"close": "closed", "comment": "commented on", "review": "reviewed"}[sub]
        return [SideEffect("github", f"{verb} {label}{where}", False, (sub[:5],))]
    if group == "release" and sub in ("create", "upload", "edit", "delete"):
        tag = tok(rest[0], 30) if rest else ""
        verb = {
            "create": "published release",
            "upload": "uploaded to release",
            "edit": "edited release",
            "delete": "deleted release",
        }[sub]
        high = sub in ("create", "delete")
        return [
            SideEffect(
                "release", f"{verb} {tag}{where}".replace("  ", " ").rstrip(), high, ("release",)
            )
        ]
    if group == "repo" and sub in ("create", "delete", "archive", "rename", "edit"):
        name = tok(rest[0]) if rest and sub != "rename" else ""
        verb = {
            "create": "created repo",
            "delete": "deleted repo",
            "archive": "archived repo",
            "rename": "renamed repo",
            "edit": "changed repo settings",
        }[sub]
        return [
            SideEffect(
                "repo",
                f"{verb} {name}{where}".replace("  ", " ").rstrip(),
                sub == "delete",
                ("repo", sub[:5]),
            )
        ]
    if group == "workflow" and sub == "run":
        wf = tok(rest[0]) if rest else "a workflow"
        return [SideEffect("deploy", f"ran workflow {wf}{where}", False, ("workflow",))]
    if group == "secret" and sub in ("set", "delete", "remove"):
        name = tok(rest[0]) if rest else ""
        verb = "set" if sub == "set" else "deleted"
        return [
            SideEffect(
                "github", f"{verb} GitHub secret {name}{where}".rstrip(), sub != "set", ("secret",)
            )
        ]
    if group == "issue" and sub in ("create", "comment", "close", "delete"):
        verb = {
            "create": "opened an issue",
            "comment": "commented on an issue",
            "close": "closed an issue",
            "delete": "deleted an issue",
        }[sub]
        return [SideEffect("github", f"{verb}{where}", sub == "delete", ("issue",))]
    if group == "api":
        method = (_opt(args, "-X", "--method") or "").upper()
        fields = _has(args, "-f", "-F", "--field", "--raw-field", "--input")
        if not method:
            method = "POST" if fields else "GET"
        if method not in _WRITE_METHODS:
            return []
        path = tok(sub.split("?", 1)[0], 50) if sub else "?"
        return [
            SideEffect("http", f"gh api {method} {path}", method == "DELETE", (method.lower(),))
        ]
    return []


def _pr_number(rest: Sequence[str]) -> str | None:
    for x in rest:
        m = re.fullmatch(r"#?([0-9]{1,7})", x) or re.search(r"/pull/([0-9]{1,7})", x)
        if m:
            return m[1]
    return None


# --- deploys and publishing ---------------------------------------------------------------


def _deploy(head: str, args: list[str]) -> list[SideEffect]:
    pos = _positional(
        args,
        (
            "-a",
            "--app",
            "-c",
            "--config",
            "-f",
            "--file",
            "-n",
            "--namespace",
            "--context",
            "--project",
            "--region",
            "-e",
            "--env",
            "--scope",
            "--token",
            "-t",
            "--target",
            "--stage",
            "-s",
            "--profile",
        ),
    )
    sub = pos[0] if pos else ""
    app = _opt(args, "-a", "--app")
    app_part = f" ({tok(app)})" if app else ""

    def eff(what: str, high: bool = False, *keys: str) -> list[SideEffect]:
        return [SideEffect("deploy", what, high, keys or ("deploy",))]

    if head in ("fly", "flyctl") and sub == "deploy":
        return eff(f"deployed with fly{app_part}")
    if head == "vercel":
        prod = _has(args, "--prod")
        if sub in ("", "deploy") or prod:
            return eff("deployed to vercel" + (" prod" if prod else ""), False)
        if sub in ("rm", "remove"):
            return eff("removed a vercel deployment", True, "remov")
        if sub == "promote":
            return eff("promoted a vercel deployment")
    if head in ("netlify", "ntl") and sub == "deploy":
        return eff("deployed to netlify" + (" prod" if _has(args, "--prod") else ""))
    if head in ("kubectl", "oc") and sub in (
        "apply",
        "create",
        "delete",
        "replace",
        "patch",
        "scale",
        "rollout",
        "set",
        "edit",
        "drain",
    ):
        ctx = _opt(args, "--context")
        on = f" on {tok(ctx)}" if ctx else ""
        return eff(f"kubectl {sub}{on}", sub in ("delete", "drain"), "kubectl")
    if head == "helm" and sub in ("install", "upgrade", "uninstall", "rollback", "delete"):
        rel = tok(pos[1]) if len(pos) > 1 else ""
        return eff(f"helm {sub} {rel}".rstrip(), sub in ("uninstall", "delete"), "helm")
    if head in ("terraform", "tofu") and sub in ("apply", "destroy", "import"):
        return eff(f"{head} {sub}", sub == "destroy", head, sub[:5])
    if head == "pulumi" and sub in ("up", "update", "destroy"):
        return eff(f"pulumi {sub}", sub == "destroy", "pulumi")
    if head in ("docker", "podman") and sub == "push" and len(pos) > 1:
        return eff(f"pushed image {tok(pos[1].split('@', 1)[0])}", False, "push", "image")
    if head in ("docker", "podman") and sub in ("buildx", "build") and _has(args, "--push"):
        return eff("built and pushed an image", False, "push", "image")
    if head == "wrangler" and sub in ("deploy", "publish"):
        return eff("deployed with wrangler")
    if head == "firebase" and sub == "deploy":
        return eff("deployed to firebase")
    if head == "railway" and sub in ("up", "deploy"):
        return eff("deployed to railway")
    if head == "heroku" and sub.startswith(
        ("releases:rollback", "ps:scale", "config:set", "apps:destroy")
    ):
        return eff(f"heroku {tok(sub)}{app_part}", sub.startswith("apps:destroy"), "heroku")
    if head in ("serverless", "sls") and sub in ("deploy", "remove"):
        return eff(f"serverless {sub}", sub == "remove", "serverless")
    if head == "cdk" and sub in ("deploy", "destroy"):
        return eff(f"cdk {sub}", sub == "destroy", "cdk")
    if head == "gcloud" and "deploy" in pos:
        return eff("deployed with gcloud " + " ".join(tok(p, 20) for p in pos[:2]))
    if head == "aws" and len(pos) > 1:
        svc, op = pos[0], pos[1]
        if svc == "s3" and op in ("cp", "sync", "mv", "rm", "rb"):
            dst = next((p for p in reversed(pos[2:]) if p.startswith("s3://")), None)
            bucket = f" {tok(dst[5:].split('/', 1)[0])}" if dst else ""
            return eff(f"aws s3 {op}{bucket}", op in ("rm", "rb"), "s3")
        if op.startswith(("deploy", "update-function", "update-service", "delete-")):
            return eff(f"aws {tok(svc, 20)} {tok(op, 30)}", op.startswith("delete-"), "aws")
    return []


def _publish(head: str, args: list[str]) -> list[SideEffect]:
    pos = _positional(
        args,
        (
            "--tag",
            "--access",
            "--otp",
            "-r",
            "--repository",
            "--token",
            "--index",
            "--publish-url",
            "-u",
            "--username",
            "-p",
            "--password",
            "--registry",
            "--repository-url",
        ),
    )
    sub = pos[0] if pos else ""
    if head in ("npm", "pnpm", "bun") and sub == "publish":
        return [SideEffect("publish", f"published to npm ({head} publish)", True, ("publish",))]
    if head == "yarn" and (sub == "publish" or pos[:2] == ["npm", "publish"]):
        return [SideEffect("publish", "published to npm (yarn publish)", True, ("publish",))]
    if head in ("uv", "poetry", "hatch", "flit", "pdm") and sub == "publish":
        return [SideEffect("publish", f"published to PyPI ({head} publish)", True, ("publish",))]
    if head == "twine" and sub == "upload":
        return [SideEffect("publish", "published to PyPI (twine upload)", True, ("publish",))]
    if head == "cargo" and sub == "publish":
        return [SideEffect("publish", "published a crate (cargo publish)", True, ("publish",))]
    if head == "gem" and sub == "push":
        return [SideEffect("publish", "published a gem", True, ("publish",))]
    return []


# --- HTTP ----------------------------------------------------------------------------------

_CURL_VALUE = (
    "-X",
    "--request",
    "-d",
    "--data",
    "--data-raw",
    "--data-binary",
    "--data-urlencode",
    "--json",
    "-F",
    "--form",
    "-T",
    "--upload-file",
    "-H",
    "--header",
    "-u",
    "--user",
    "-o",
    "--output",
    "-A",
    "--user-agent",
    "-e",
    "--referer",
    "-b",
    "--cookie",
    "-c",
    "--cookie-jar",
    "-m",
    "--max-time",
    "--connect-timeout",
    "-w",
    "--write-out",
    "--retry",
    "-K",
    "--config",
    "-x",
    "--proxy",
    "--cacert",
    "--cert",
    "--key",
    "--oauth2-bearer",
    "--url",
)
_CURL_DATA = (
    "-d",
    "--data",
    "--data-raw",
    "--data-binary",
    "--data-urlencode",
    "--json",
    "-F",
    "--form",
)


def _url_arg(pos: Sequence[str], args: Sequence[str]) -> str | None:
    explicit = _opt(args, "--url")
    if explicit:
        return explicit
    for p in pos:
        if "://" in p or re.match(r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+(:[0-9]+)?(/|$)", p):
            return p
    return None


def _http_effect(method: str, url: str | None, tool: str) -> list[SideEffect]:
    host = host_of(url) if url else None
    if host in _LOCAL_HOSTS:
        return []  # a local dev server, not the outside world
    shown = host or "?"
    return [
        SideEffect(
            "http",
            f"{method} {shown} ({tool})",
            method == "DELETE",
            (shown.lower(),) if host else (method.lower(),),
        )
    ]


def _curl(args: list[str]) -> list[SideEffect]:
    method = (_opt(args, "-X", "--request") or "").upper()
    if not method:
        if _has(args, "-T", "--upload-file"):
            method = "PUT"
        elif _has(args, *_CURL_DATA) and not _has(args, "-G", "--get"):
            method = "POST"
    if method not in _WRITE_METHODS:
        return []
    return _http_effect(method, _url_arg(_positional(args, _CURL_VALUE), args), "curl")


def _httpie(args: list[str]) -> list[SideEffect]:
    pos = _positional(
        args,
        (
            "-a",
            "--auth",
            "-A",
            "--auth-type",
            "-o",
            "--output",
            "--session",
            "--verify",
            "--cert",
            "--cert-key",
            "--timeout",
        ),
    )
    method = ""
    if pos and pos[0].upper() in {"GET", "HEAD", "OPTIONS", *_WRITE_METHODS}:
        method, pos = pos[0].upper(), pos[1:]
    url = pos[0] if pos else None
    items = pos[1:]
    if not method:
        writes = any(re.match(r"[^=:@]+(:=|=|@)", i) and "==" not in i for i in items)
        method = "POST" if writes or _has(args, "-f", "--form", "--raw") else "GET"
    if method not in _WRITE_METHODS:
        return []
    return _http_effect(method, url, "httpie")


def _wget(args: list[str]) -> list[SideEffect]:
    method = (_opt(args, "--method") or "").upper()
    if not method and _has(args, "--post-data", "--post-file", "--body-data", "--body-file"):
        method = "POST"
    if method not in _WRITE_METHODS:
        return []
    pos = _positional(
        args,
        (
            "-O",
            "--output-document",
            "-o",
            "--header",
            "--body-data",
            "--post-data",
            "--user",
            "--password",
            "-U",
        ),
    )
    return _http_effect(method, _url_arg(pos, args), "wget")


# --- messages and Resonant -----------------------------------------------------------------


def _message(head: str, args: list[str], raw: str) -> list[SideEffect]:
    if head in ("mail", "mailx", "sendmail", "mutt", "neomutt", "msmtp", "s-nail"):
        return [SideEffect("message", f"sent an email ({head})", False, ("email",))]
    if head == "himalaya" and "send" in args:
        return [SideEffect("message", "sent an email (himalaya)", False, ("email",))]
    if head == "osascript":
        script = " ".join(args)
        if re.search(r"application\s+\\?\"?(Messages|iChat)", script, re.IGNORECASE):
            return [
                SideEffect(
                    "message", "sent a message with Messages (osascript)", True, ("message",)
                )
            ]
        if re.search(r"application\s+\\?\"?Mail\b", script) and re.search(r"\bsend\b", script):
            return [SideEffect("message", "sent an email with Mail (osascript)", False, ("email",))]
    if head == "slack" and any(a in ("send", "post") for a in args):
        return [SideEffect("message", "sent a Slack message", False, ("slack",))]
    if head == "twilio" and "messages" in raw.lower():
        return [SideEffect("message", "sent an SMS (twilio)", False, ("sms",))]
    return []


def _resonant(args: list[str]) -> list[SideEffect]:
    pos = _positional(args)
    if pos and pos[0] == "restart":
        return [SideEffect("self", "ran resonant restart", False, ("restart",))]
    if pos and pos[0] == "update":
        return [SideEffect("self", "ran resonant update", False, ("update",))]
    return []


# --- entry points --------------------------------------------------------------------------

_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "fish"})


def classify_command(cmd: str, ctx: RunContext | None = None) -> list[SideEffect]:
    """The outside actions in one shell command line, in order, without repeats."""
    return _dedupe(_classify(cmd, ctx or RunContext(), 0))


def _classify(cmd: str, ctx: RunContext, depth: int) -> list[SideEffect]:
    out: list[SideEffect] = []
    for argv in commands(cmd):
        a = unwrap(argv)
        if not a:
            continue
        head, args = _base(a[0]), a[1:]
        if head in _SHELLS and "-c" in args and depth < _MAX_DEPTH:
            i = args.index("-c")
            if i + 1 < len(args):
                out += _classify(args[i + 1], ctx, depth + 1)
            continue
        if head == "eval" and depth < _MAX_DEPTH:
            out += _classify(" ".join(args), ctx, depth + 1)
            continue
        if head == "git":
            out += _git(args, ctx)
        elif head == "gh":
            out += _gh(args)
        elif head == "curl":
            out += _curl(args)
        elif head in ("http", "https", "xh", "xhs"):
            out += _httpie(args)
        elif head == "wget":
            out += _wget(args)
        elif head == "resonant":
            out += _resonant(args)
        else:
            out += _publish(head, args) or _deploy(head, args) or _message(head, args, cmd)
    return out


def classify_tool(
    name: str, tool_input: Mapping[str, Any], ctx: RunContext | None = None
) -> list[SideEffect]:
    """The outside actions in one tool call of a stream-json ``tool_use`` block: ``Bash``
    commands, and MCP tools whose name says they write."""
    if name == "Bash":
        cmd = tool_input.get("command")
        return classify_command(cmd, ctx) if isinstance(cmd, str) else []
    if name.startswith("mcp__"):
        parts = name.split("__")
        server, tool = (parts[1], "__".join(parts[2:])) if len(parts) > 2 else ("?", name)
        m = _MCP_WRITE.search(tool)
        if m is None:
            return []
        high = _MCP_HIGH.search(tool) is not None
        return [
            SideEffect(
                "mcp",
                f"used {tok(server, 30)} {tok(tool, 40)}",
                high,
                (tok(tool, 40).lower(),),
            )
        ]
    return []


def tool_calls(message: Mapping[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """``(id, name, input)`` of each ``tool_use`` block in a stream-json ``assistant``
    message; [] for anything else."""
    if message.get("type") != "assistant":
        return []
    inner: object = message.get("message")
    content: object = (
        cast(dict[str, object], inner).get("content") if isinstance(inner, dict) else None
    )
    if not isinstance(content, list):
        return []
    out: list[tuple[str, str, dict[str, Any]]] = []
    for item in cast(list[object], content):
        if not isinstance(item, dict):
            continue
        block = cast(dict[str, object], item)
        name, args, bid = block.get("name"), block.get("input"), block.get("id")
        if block.get("type") == "tool_use" and isinstance(name, str) and isinstance(args, dict):
            out.append((bid if isinstance(bid, str) else "", name, cast(dict[str, Any], args)))
    return out


def detect(message: Mapping[str, Any], ctx: RunContext | None = None) -> list[SideEffect]:
    """Every outside action in a stream-json message's tool calls."""
    out: list[SideEffect] = []
    for _id, name, args in tool_calls(message):
        out += classify_tool(name, args, ctx)
    return _dedupe(out)


def reported(effect: SideEffect, did: Iterable[str]) -> bool:
    """Whether a ``did:`` item already names ``effect`` (all its keys in one item)."""
    keys = [k.lower() for k in effect.keys if k]
    if not keys:
        return False
    return any(all(k in item.lower() for k in keys) for item in did)


def _dedupe(effects: Iterable[SideEffect]) -> list[SideEffect]:
    seen: set[str] = set()
    out: list[SideEffect] = []
    for e in effects:
        what = e.what[:WHAT_MAX]
        if what in seen:
            continue
        seen.add(what)
        out.append(e if what == e.what else SideEffect(e.kind, what, e.high, e.keys))
    return out


__all__ = [
    "RunContext",
    "SideEffect",
    "classify_command",
    "classify_tool",
    "commands",
    "detect",
    "host_of",
    "reported",
    "tool_calls",
    "unwrap",
]
