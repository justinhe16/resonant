"""PER-258: the shell target family's rules (``gate/shell.py``), on their own.

Every case runs in a scratch tree under ``tmp_path``: a fake ``RESONANT_HOME`` with the
brain's own dir in it, a read root holding a git repo on a feature branch, and a linked
worktree of it. Nothing runs a command: the policy reads ``.git`` files only.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from resonant.gate.shell import ASK_RULES, READ_RULES, ShellPolicy, git_facts
from resonant_sdk import Level


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, timeout=30)


@pytest.fixture
def tree(tmp_path: Path) -> dict[str, Path]:
    home = tmp_path / "home"
    rh = home / ".resonant"
    (rh / "ask" / "session").mkdir(parents=True)
    code = home / "code"
    repo = code / "r"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "--allow-empty",
        "-m", "x")  # fmt: skip
    git(repo, "checkout", "-q", "-b", "feat")
    wt = code / "r-wt"
    git(repo, "worktree", "add", "-q", "-b", "wt-branch", str(wt))
    (repo / ".env").write_text("SECRET=1")
    return {"home": home, "rh": rh, "code": code, "repo": repo, "wt": wt}


@pytest.fixture
def policy(tree: dict[str, Path]) -> ShellPolicy:
    code = os.path.realpath(tree["code"])

    def read_ok(path: str) -> bool:
        real = os.path.realpath(path)
        return (real == code or real.startswith(code + "/")) and ".env" not in Path(real).name

    return ShellPolicy(read_ok=read_ok, home=tree["rh"], scratch=[tree["rh"] / "ask"],
                       user_home=tree["home"], daemon_pid=4242, temps=[])  # fmt: skip


def judge(policy: ShellPolicy, command: str, cwd: Path) -> tuple[str, Level, str]:
    v = policy.judge({"tool": "Bash", "command": command, "cwd": str(cwd)})
    return v.kind, v.level, v.verb


@pytest.mark.parametrize(
    ("command", "verb"),
    [
        ("git rev-list --count HEAD", "git-rev-list"),
        ("git log --oneline -5", "git-log"),
        ("git status 2>/dev/null", "git-status"),
        ("git branch", "git-branch"),
        ("git branch -a -v", "git-branch"),
        ("git remote -v", "git-remote"),
        ("git log | head -3", "git-log"),
        ("ls -la && wc -l README", "ls"),
        ("gh pr view 12", "gh-pr-view"),
        ("gh run list --limit 3", "gh-run-list"),
        ("rg TODO .", "rg"),
        ("find . -name '*.py'", "find"),
        ("date -u", "date"),
        ("ps aux", "ps"),
        ("ps -ax -o pid,command", "ps"),
        ("resonant status", "resonant-status"),
    ],
)
def test_reads(policy: ShellPolicy, tree: dict[str, Path], command: str, verb: str) -> None:
    assert judge(policy, command, tree["repo"]) == ("read", Level.L0, verb)


@pytest.mark.parametrize(
    "command",
    [
        "git log && git push",  # a compound write never counts as a read
        "git status; touch x",
        "git log $(touch x)",
        "git log `touch x`",
        "cat <(touch x)",
        "git log > out.txt",
        "git log | tee out.txt",
        "git diff --output=x",
        "git log -1 --output=x",
        "git branch -D feat",
        "git remote add evil https://x",
        "git -c core.pager=touch log",
        "rg --pre touch foo .",
        "find . -delete",
        "find . -exec touch x \\;",
        "find . -fprint x",
        "FOO=1 git log",
        "env git log",
        "date -s 1200",
        "ps -E",  # every process's environment (tokens)
        "ps eww -ax",
        "ps -ef",
        "git log <<EOF\nx\nEOF",
    ],
)
def test_a_read_prefix_cant_smuggle_a_write(
    policy: ShellPolicy, tree: dict[str, Path], command: str
) -> None:
    kind, level, _ = judge(policy, command, tree["repo"])
    assert kind == "write" and level is Level.L2


@pytest.mark.parametrize(
    ("command", "level", "verb"),
    [
        ("git add -A", Level.L3, "git-add"),
        ("git commit -m 'fix the security bug'", Level.L3, "git-commit"),
        ("git checkout -b fix-x", Level.L3, "git-checkout"),
        ("git branch new-one", Level.L3, "git-branch"),
        ("git push", Level.L3, "git-push"),  # the current branch, feat
        ("git push -u origin feat", Level.L3, "git-push"),
        ("git push origin HEAD", Level.L3, "git-push"),
        ("gh pr create --fill", Level.L3, "gh-pr-create"),
        ("git push origin main", Level.L2, "git-push"),
        ("git push origin feat:main", Level.L2, "git-push"),
        ("git push origin +feat", Level.L2, "git-push"),
        ("git push origin :feat", Level.L2, "git-push"),
        ("git push -f", Level.L2, "git-push-force"),
        ("git push --force-with-lease", Level.L2, "git-push-force"),
        ("git push --delete origin feat", Level.L2, "git-push-force"),
        ("gh pr merge 3 --squash", Level.L2, "gh-pr-merge"),
        ("git reset --hard", Level.L2, "git-reset"),
        ("git checkout -- README", Level.L2, "git-checkout"),
        ("rm -rf build", Level.L2, "rm"),
        ("launchctl kickstart -k gui/501/com.resonant.daemon", Level.L2, "launchctl"),
        ("resonant restart", Level.L2, "resonant-restart"),
        ("npm install", Level.L2, "npm"),
        ("uv sync", Level.L2, "uv"),
        ("./scripts/deploy.sh", Level.L2, "deploy.sh"),
        ("git add -A && git commit -m x", Level.L2, "compound"),
    ],
)
def test_write_levels(
    policy: ShellPolicy, tree: dict[str, Path], command: str, level: Level, verb: str
) -> None:
    assert judge(policy, command, tree["repo"]) == ("write", level, verb)


def test_a_push_from_main_is_to_the_default_branch(
    policy: ShellPolicy, tree: dict[str, Path]
) -> None:
    git(tree["repo"], "checkout", "-q", "main")
    assert judge(policy, "git push", tree["repo"])[1] is Level.L2
    assert judge(policy, "git push origin HEAD", tree["repo"])[1] is Level.L2


def test_a_push_outside_a_repo_asks(policy: ShellPolicy, tree: dict[str, Path]) -> None:
    assert judge(policy, "git push origin feat", tree["rh"] / "ask")[1] is Level.L2


@pytest.mark.parametrize(
    "command",
    [
        "resonant secret set x",
        "resonant secrets list",
        "/opt/bin/resonant ext add ./x",
        "resonant ext update github",
        "uv run resonant secret set x",
        "python -m resonant secret set x",
        "env FOO=1 resonant secret set x",
        "sudo -u root security dump-keychain",
        "security find-generic-password -s x",
        "git log && security delete-keychain x",
        "sh -c 'resonant ext add foo'",
        'bash -c "echo hi; security list-keychains"',
        "eval resonant secret set x",
        "pkill -f resonant",
        "killall Resonant",
        "kill -9 4242",
        "kill -9 -1",
        "launchctl bootout gui/501/com.resonant.daemon",
        "launchctl stop com.resonant.daemon",
        "echo x > ~/.resonant/config.yaml",
        "echo x >> $HOME/.resonant/config.yaml",
        "cp evil.yaml ~/.resonant/config.yaml",
        "sed -i '' s/on/off/ ~/.resonant/config.yaml",
        "rm -rf ~/.resonant",
        "git -C ~/.resonant init",
        "cp x ~/Library/Keychains/login.keychain-db",
        "resonant secret set 'unbalanced",  # doesn't tokenize: the raw text still matches
    ],
)
def test_the_never_list(policy: ShellPolicy, tree: dict[str, Path], command: str) -> None:
    v = policy.judge({"tool": "Bash", "command": command, "cwd": str(tree["repo"])})
    assert v.kind == "never", command
    assert policy.refusal({"tool": "Bash", "command": command, "cwd": str(tree["repo"])})


def test_a_write_from_inside_resonant_home_is_never_allowed(
    policy: ShellPolicy, tree: dict[str, Path]
) -> None:
    assert judge(policy, "touch x", tree["rh"])[0] == "never"
    assert judge(policy, f"cd {tree['rh']} && touch x", tree["repo"])[0] == "never"
    # the brain's own dir is its scratch space
    assert judge(policy, "touch x", tree["rh"] / "ask" / "session") == ("write", Level.L2, "touch")
    assert policy.judge({"tool": "Write", "command": str(tree["rh"] / "ask" / "n.txt"),
                         "cwd": "/"}).level is Level.L3  # fmt: skip


def test_kill_of_another_process_asks(policy: ShellPolicy, tree: dict[str, Path]) -> None:
    assert judge(policy, "kill 999", tree["repo"]) == ("write", Level.L2, "kill")


@pytest.mark.parametrize(
    "command",
    ["cat /etc/hosts", "cat .env", "cat ~/.ssh/id_rsa", "ls ~", "cd /etc", "cat ../../x",
     "grep -r password ~/.aws", "cat *.env"],
)  # fmt: skip
def test_reads_outside_the_read_roots_are_refused(
    policy: ShellPolicy, tree: dict[str, Path], command: str
) -> None:
    v = policy.judge({"tool": "Bash", "command": command, "cwd": str(tree["repo"])})
    assert v.kind == "never" and "outside the folders" in v.why


def test_file_tools(policy: ShellPolicy, tree: dict[str, Path]) -> None:
    def level(tool: str, path: Path) -> tuple[str, Level]:
        v = policy.judge({"tool": tool, "command": str(path), "cwd": "/"})
        return v.kind, v.level

    assert level("Edit", tree["wt"] / "a.py") == ("write", Level.L3)  # a linked worktree
    assert level("Write", tree["repo"] / "a.py") == ("write", Level.L2)  # the main checkout
    assert level("Edit", tree["wt"] / ".git") == ("write", Level.L2)
    assert level("Edit", tree["rh"] / "config.yaml")[0] == "never"
    assert level("Read", tree["home"] / "x")[0] == "never"  # outside the dirs: refused
    assert level("WebFetch", tree["home"])[0] == "read"


def test_request_folds_a_leading_cd_and_names_the_repo(
    policy: ShellPolicy, tree: dict[str, Path]
) -> None:
    call = policy.request("Bash", {"command": f"cd {tree['repo']} && git push origin feat"},
                          "/", lambda s: s.replace("feat", "[redacted]"))  # fmt: skip
    assert call.args == {
        "tool": "Bash",
        "command": "git push origin feat",
        "cwd": str(tree["repo"]),
        "verb": "git-push",
        "shown": "git push origin [redacted]",
        "repo": "r",
    }
    assert call.spec == "shell" and call.verdict.level is Level.L3
    wt = policy.request("Bash", {"command": "git commit -m x"}, str(tree["wt"]), str)
    assert wt.args["repo"] == "r"  # a worktree is named after its main repo
    edit = policy.request("Edit", {"file_path": "a.py", "old_string": "a", "new_string": "b"},
                          str(tree["wt"]), str)  # fmt: skip
    other = policy.request("Edit", {"file_path": "a.py", "old_string": "a", "new_string": "c"},
                           str(tree["wt"]), str)  # fmt: skip
    assert edit.args["command"] == str(tree["wt"] / "a.py")
    assert edit.args["digest"] != other.args["digest"]  # an approval covers one exact edit


def test_summary_shows_the_command_and_where(policy: ShellPolicy, tree: dict[str, Path]) -> None:
    args = {"tool": "Bash", "shown": "git push origin main", "cwd": str(tree["repo"])}
    assert policy.summary(args) == "run `git push origin main` in ~/code/r"
    assert policy.summary({"tool": "Write", "shown": str(tree["home"] / "x"), "cwd": "/"}) == (
        "write ~/x"
    )


def test_cwd_after(policy: ShellPolicy, tree: dict[str, Path]) -> None:
    repo = str(tree["repo"])
    assert policy.cwd_after(f"cd {repo}", "/", "") == repo
    assert policy.cwd_after(f"cd {repo} && git log", "/", "") == repo
    assert policy.cwd_after("git log", repo, "") == repo
    assert policy.cwd_after("ls", repo, "x\nShell cwd was reset to /Users/x/ask") == "/Users/x/ask"


def test_did(policy: ShellPolicy, tree: dict[str, Path]) -> None:
    base = {"cwd": str(tree["repo"]), "repo": "r", "tool": "Bash"}
    assert policy.did({**base, "verb": "git-push", "shown": "git push"}, "") == "pushed feat in r"
    pr = "https://github.com/o/r/pull/120\n"
    assert policy.did({**base, "verb": "gh-pr-create", "shown": "gh pr create"}, pr) == (
        "opened PR #120 in r"
    )
    assert (
        policy.did({**base, "verb": "rm", "shown": "rm -rf build"}, "") == "ran `rm -rf build` in r"
    )


def test_git_facts(tree: dict[str, Path]) -> None:
    facts = git_facts(str(tree["repo"] / "sub" / "dir"))
    assert facts is not None and (facts.name, facts.branch, facts.linked) == ("r", "feat", False)
    wt = git_facts(str(tree["wt"]))
    assert wt is not None and (wt.name, wt.branch, wt.linked) == ("r", "wt-branch", True)
    assert git_facts(str(tree["rh"])) is None


def test_the_allowlist_rules_have_no_writing_prefix() -> None:
    """Each pre-allowed rule's prefix is a read (Claude Code matches the prefix only), and
    the forms of those prefixes that write are ``ask`` rules."""
    for rule in READ_RULES:
        assert rule.startswith("Bash(") and rule.endswith(")")
        body = rule[5:-1].removesuffix(":*")
        assert body.split()[0] in {"git", "gh", "ls", "wc", "cat", "head", "tail", "find",
                                   "rg", "grep", "du", "df", "ps", "date", "which",
                                   "resonant"}  # fmt: skip
        assert body not in ("git branch:*", "git remote")  # exact forms only
    asked = " ".join(ASK_RULES)
    for flag in ("--output", "--pre", "-delete", "-exec", "-fprint"):
        assert flag in asked
