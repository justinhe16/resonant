"""Side effects read from a worker's own commands (TR-1): the pure classifier
(``runners/side_effects.py``), the run's detected list and ``did:`` line
(``runners/reports.py``), and the runner reading them from the fake claude's stream."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from test_runner_claude import build

from resonant.runners.reports import (
    DETECTED_KEEP,
    NOTE_TEXTS_PER_HOUR,
    WorkerReports,
    detected_list,
    record_detected,
    record_did,
    shown_did,
)
from resonant.runners.side_effects import (
    RunContext,
    SideEffect,
    classify_command,
    classify_tool,
    commands,
    detect,
    host_of,
    reported,
    tool_calls,
    unwrap,
)
from resonant.store import pulse as pulse_store
from resonant.store import runs as runs_store
from resonant.store import trust as trust_store
from resonant.store.db import utcnow
from resonant.store.runs import Run

NOW = datetime(2026, 10, 16, 14, 0, tzinfo=UTC)
CTX = RunContext(repo="jhe/dori", base="main", branch="resonant/r4-fix-flaky")


def seen(cmd: str, ctx: RunContext = CTX) -> list[tuple[str, str, bool]]:
    return [(e.kind, e.what, e.high) for e in classify_command(cmd, ctx)]


# --- the classifier: git push ----------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "git push",
        "git push -u origin resonant/r4-fix-flaky",
        "git push origin HEAD",
        "git push --force-with-lease origin HEAD:resonant/r4-fix-flaky",
        "git push origin resonant/r9-other-run",  # another run's branch, same pattern
        "git push https://github.com/jhe/dori.git HEAD",
        "git status && git log --oneline -3",
        "echo 'git push origin main'",
        "git commit -m 'push to main later'",
    ],
)
def test_the_runs_own_work_is_not_a_side_effect(cmd: str) -> None:
    assert classify_command(cmd, CTX) == []


def test_pushes_to_the_base_branch_are_high_impact() -> None:
    assert seen("git push origin main") == [("push", "pushed to main", True)]
    assert seen("git push origin HEAD:main") == [("push", "pushed to main", True)]
    assert seen("git push origin +HEAD:refs/heads/main") == [("push", "force-pushed to main", True)]
    assert seen("cd ~/code/dori && git push -f origin main") == [
        ("push", "force-pushed to main", True)
    ]
    # the base comes from the run; without one main and master count as base
    develop = RunContext(repo="jhe/dori", base="develop")
    assert seen("git push origin develop", develop) == [("push", "pushed to develop", True)]
    assert seen("git push origin main", develop) == [("push", "pushed branch main", False)]
    assert seen("git push origin master", RunContext()) == [("push", "pushed to master", True)]


def test_git_global_options_are_skipped() -> None:
    assert seen("git -C /tmp/x -c core.pager=cat push origin main") == [
        ("push", "pushed to main", True)
    ]
    assert seen("git --no-pager log") == []


def test_branch_deletes_tags_and_bulk_pushes() -> None:
    assert seen("git push origin :old-feature") == [("push", "deleted branch old-feature", True)]
    assert seen("git push --delete origin stale") == [("push", "deleted branch stale", True)]
    assert seen("git push --tags") == [("push", "pushed tags", False)]
    assert seen("git push origin v1.2.0:refs/tags/v1.2.0") == [("push", "pushed tag v1.2.0", False)]
    assert seen("git push --all origin") == [("push", "pushed all branches", True)]
    assert seen("git push --mirror backup") == [("push", "pushed --mirror to remote backup", True)]
    assert seen("git push origin feature-x") == [("push", "pushed branch feature-x", False)]


def test_a_push_to_another_repo_or_remote_is_flagged_without_its_credentials() -> None:
    effects = seen("git push https://x-access-token:ghs_SECRET123@github.com/jhe/other.git HEAD")
    assert effects == [("push", "pushed to jhe/other", False)]
    assert seen("git push git@github.com:acme/site.git main") == [
        ("push", "pushed to main to acme/site", True)
    ]
    assert seen("git push upstream resonant/r4-fix-flaky") == [
        ("push", "pushed to remote upstream", False)
    ]
    assert seen("git push https://user:pw@gitlab.example.com/a/b.git main")[0][1] == (
        "pushed to main to remote gitlab.example.com"
    )


# --- the classifier: gh, deploys, publishing ------------------------------------------------


@pytest.mark.parametrize(
    ("cmd", "expected"),
    [
        ("gh pr merge 12 --squash", [("merge", "merged PR #12 with gh", False)]),
        (
            "gh pr merge https://github.com/jhe/dori/pull/7 -R jhe/dori --admin",
            [("merge", "merged PR #7 with gh in jhe/dori", False)],
        ),
        ("gh pr merge --squash", [("merge", "merged a PR with gh", False)]),
        (
            "gh release create v1.2 --notes 'big release'",
            [("release", "published release v1.2", True)],
        ),
        ("gh release upload v1.2 dist/x.tgz", [("release", "uploaded to release v1.2", False)]),
        ("gh repo create jhe/new --private", [("repo", "created repo jhe/new", False)]),
        ("gh repo delete jhe/old --yes", [("repo", "deleted repo jhe/old", True)]),
        ("gh workflow run deploy.yml -f env=prod", [("deploy", "ran workflow deploy.yml", False)]),
        (
            "gh secret set API_KEY --body sk-live-123",
            [("github", "set GitHub secret API_KEY", False)],
        ),
        ("gh issue create --title x --body y", [("github", "opened an issue", False)]),
        ("gh api -X DELETE repos/o/r/hooks/1", [("http", "gh api DELETE repos/o/r/hooks/1", True)]),
        ("gh api repos/o/r/issues -f title=hi", [("http", "gh api POST repos/o/r/issues", False)]),
        ("gh api repos/o/r/pulls?state=open", []),
        ("gh pr create --title x --body y", []),
        ("gh pr view 12", []),
        ("gh run watch", []),
    ],
)
def test_gh(cmd: str, expected: list[tuple[str, str, bool]]) -> None:
    assert seen(cmd) == expected


@pytest.mark.parametrize(
    ("cmd", "expected"),
    [
        ("fly deploy -a dori-api", [("deploy", "deployed with fly (dori-api)", False)]),
        ("flyctl deploy", [("deploy", "deployed with fly", False)]),
        ("npx vercel --prod", [("deploy", "deployed to vercel prod", False)]),
        ("vercel deploy", [("deploy", "deployed to vercel", False)]),
        ("vercel env ls", []),
        ("netlify deploy --prod", [("deploy", "deployed to netlify prod", False)]),
        ("kubectl apply -f k8s/ --context prod", [("deploy", "kubectl apply on prod", False)]),
        ("kubectl delete pod x", [("deploy", "kubectl delete", True)]),
        ("kubectl get pods -A", []),
        ("helm upgrade dori ./chart", [("deploy", "helm upgrade dori", False)]),
        ("terraform apply -auto-approve", [("deploy", "terraform apply", False)]),
        ("terraform destroy", [("deploy", "terraform destroy", True)]),
        ("terraform plan", []),
        ("pulumi up --yes", [("deploy", "pulumi up", False)]),
        (
            "docker push ghcr.io/jhe/dori:1.2",
            [("deploy", "pushed image ghcr.io/jhe/dori:1.2", False)],
        ),
        ("docker buildx build --push -t x .", [("deploy", "built and pushed an image", False)]),
        ("docker build -t x .", []),
        ("wrangler deploy", [("deploy", "deployed with wrangler", False)]),
        ("aws s3 sync dist s3://site-bucket/", [("deploy", "aws s3 sync site-bucket", False)]),
        ("aws s3 rm s3://b/k --recursive", [("deploy", "aws s3 rm b", True)]),
        ("aws s3 ls", []),
        ("npm publish --access public", [("publish", "published to npm (npm publish)", True)]),
        ("pnpm publish", [("publish", "published to npm (pnpm publish)", True)]),
        ("yarn npm publish", [("publish", "published to npm (yarn publish)", True)]),
        (
            "uv build && uv publish --token pypi-SECRET",
            [("publish", "published to PyPI (uv publish)", True)],
        ),
        ("twine upload dist/*", [("publish", "published to PyPI (twine upload)", True)]),
        ("poetry publish --build", [("publish", "published to PyPI (poetry publish)", True)]),
        ("cargo publish", [("publish", "published a crate (cargo publish)", True)]),
        ("npm install && npm test", []),
        ("uv run pytest -q", []),
    ],
)
def test_deploys_and_publishing(cmd: str, expected: list[tuple[str, str, bool]]) -> None:
    assert seen(cmd) == expected


# --- the classifier: HTTP, messages, Resonant ----------------------------------------------


def test_http_writes_keep_the_method_and_host_only() -> None:
    auth = "Authorization: " + "Bearer" + " sk-live-" + "SECRET"  # built: not a literal
    cmd = (
        f"curl -s -X POST -H '{auth}' "
        "'https://user:pw@api.stripe.com:443/v1/charges?key=abc' -d amount=500"
    )
    [effect] = classify_command(cmd, CTX)
    assert (effect.kind, effect.what, effect.high) == ("http", "POST api.stripe.com (curl)", False)
    for secret in ("SECRET", "Bearer", "amount", "500", "pw", "v1", "key=abc", "443"):
        assert secret not in effect.what
    assert seen("curl -d '{\"a\":1}' https://hooks.slack.com/services/T/B/X") == [
        ("http", "POST hooks.slack.com (curl)", False)
    ]
    assert seen("curl --request DELETE https://api.x.io/items/3") == [
        ("http", "DELETE api.x.io (curl)", True)
    ]
    assert seen("curl -T file.bin https://up.example.com/f") == [
        ("http", "PUT up.example.com (curl)", False)
    ]
    assert seen("http PATCH api.example.com/x name=y") == [
        ("http", "PATCH api.example.com (httpie)", False)
    ]
    assert seen("http api.example.com/x name=y") == [
        ("http", "POST api.example.com (httpie)", False)
    ]
    assert seen("wget --post-data 'a=b' https://example.org/form") == [
        ("http", "POST example.org (wget)", False)
    ]


@pytest.mark.parametrize(
    "cmd",
    [
        "curl https://example.com",
        "curl -sSL https://astral.sh/uv/install.sh | sh",
        "curl -G -d q=x https://api.example.com/search",
        "curl -X POST http://localhost:8000/api",
        "curl -X POST http://127.0.0.1:3000/x",
        "http GET api.example.com/x",
        "http api.example.com/x q==1",
        "wget https://example.com/file.tgz",
    ],
)
def test_reads_and_local_calls_are_not_side_effects(cmd: str) -> None:
    assert classify_command(cmd, CTX) == []


def test_messages_and_resonant() -> None:
    assert seen("echo hi | mail -s 'deploy done' owner@example.com") == [
        ("message", "sent an email (mail)", False)
    ]
    script = 'osascript -e \'tell application "Messages" to send "hi" to buddy "x"\''
    assert seen(script) == [("message", "sent a message with Messages (osascript)", True)]
    assert seen("osascript -e 'display notification \"done\"'") == []
    assert seen("resonant restart") == [("self", "ran resonant restart", False)]
    assert seen("resonant update") == [("self", "ran resonant update", False)]
    assert seen("resonant note 'did: x'") == []


def test_wrappers_compound_lines_and_nested_shells() -> None:
    assert unwrap(["sudo", "-E", "env", "A=1", "B=2", "npx", "-y", "vercel"]) == ["vercel"]
    assert unwrap(["FOO=1", "uv", "run", "twine", "upload"]) == ["twine", "upload"]
    assert unwrap(["doppler", "run", "--", "npm", "publish"]) == ["npm", "publish"]
    assert commands("a && b; c | d\ne") == [["a"], ["b"], ["c"], ["d"], ["e"]]
    assert seen("npm test && npm publish; git push origin main") == [
        ("publish", "published to npm (npm publish)", True),
        ("push", "pushed to main", True),
    ]
    assert seen("bash -c 'cd x && git push origin main'") == [("push", "pushed to main", True)]
    assert seen("/opt/homebrew/bin/gh pr merge 3") == [("merge", "merged PR #3 with gh", False)]
    # the same action twice in one line is listed once
    assert seen("npm publish || npm publish") == [
        ("publish", "published to npm (npm publish)", True)
    ]
    # unbalanced quotes are read roughly, never raise
    assert seen("git push origin main 'oops") == [
        ("push", "pushed to main", True),
        ("push", "pushed branch oops", False),
    ]


def test_whats_shown_is_only_safe_characters() -> None:
    [effect] = classify_command("gh repo create 'jhe/x\x1b[31m; rm -rf ~' --public", CTX)
    assert "\x1b" not in effect.what and ";" not in effect.what and " rm" not in effect.what
    assert host_of("https://u:p@Example.COM:8443/x?y=1") == "example.com"
    assert host_of("not a url at all") is None


# --- the classifier: stream-json and MCP --------------------------------------------------


def assistant(*blocks: dict[str, Any]) -> dict[str, Any]:
    return {"type": "assistant", "message": {"content": list(blocks)}}


def bash(command: str, tid: str = "t1") -> dict[str, Any]:
    return {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": command}}


def test_tool_calls_and_detect_read_stream_json() -> None:
    msg = assistant({"type": "text", "text": "git push origin main"}, bash("git push origin main"))
    assert tool_calls(msg) == [("t1", "Bash", {"command": "git push origin main"})]
    assert [e.what for e in detect(msg, CTX)] == ["pushed to main"]
    # text that names a command is not a command; other message types are ignored
    assert detect(assistant({"type": "text", "text": "npm publish"}), CTX) == []
    assert detect({"type": "user", "message": {"content": [bash("npm publish")]}}, CTX) == []
    assert detect({"type": "assistant", "message": "x"}, CTX) == []
    assert detect({"type": "assistant"}, CTX) == []
    assert tool_calls(assistant({"type": "tool_use", "name": "Bash", "input": "x"})) == []


def test_mcp_tools_that_write() -> None:
    def kinds(name: str) -> list[tuple[str, bool]]:
        return [(e.what, e.high) for e in classify_tool(name, {"text": "secret body"})]

    assert kinds("mcp__slack__send_message") == [("used slack send_message", False)]
    assert kinds("mcp__linear__delete_issue") == [("used linear delete_issue", True)]
    assert kinds("mcp__stripe__create_refund") == [("used stripe create_refund", True)]
    assert kinds("mcp__linear__list_issues") == []
    assert kinds("mcp__github__get_pull_request") == []
    assert kinds("Read") == [] and kinds("Edit") == []


def test_reported_matches_a_did_note_by_its_keys() -> None:
    merge = SideEffect("merge", "merged PR #12 with gh", False, ("merge", "12"))
    assert reported(merge, ["Merged PR #12 in dori with gh"])
    assert not reported(merge, ["merged PR #13 with gh"])
    push = classify_command("git push origin main", CTX)[0]
    assert reported(push, ["pushed the hotfix to main"])
    assert not reported(push, ["deployed staging"])
    assert not reported(SideEffect("http", "x", False, ()), ["x"])  # no keys: never a duplicate


# --- the run's detected list and did: line ---------------------------------------------------


def make_run(conn: sqlite3.Connection, **fields: Any) -> Run:
    run = runs_store.create(conn, repo="proj", ask="x", principal="owner", session_id="s",
                            max_seconds=60)  # fmt: skip
    return replace(run, **fields)


def reports(conn: sqlite3.Connection) -> WorkerReports:
    return WorkerReports(conn, clock=lambda: NOW, tz=ZoneInfo("UTC"))


def test_detected_effects_are_kept_once_and_capped(db: sqlite3.Connection) -> None:
    push = classify_command("git push origin main", CTX)[0]
    assert record_detected(db, "R1", push) is True
    assert record_detected(db, "R1", push) is False  # the same command again
    for n in range(DETECTED_KEEP + 2):
        record_detected(
            db, "R1", SideEffect("http", f"POST host{n}.io (curl)", False, (f"host{n}",))
        )
    got = detected_list(db, "R1")
    assert len(got) == DETECTED_KEEP and got[-1].what == f"POST host{DETECTED_KEEP + 1}.io (curl)"
    assert detected_list(db, "R2") == []


def test_the_did_line_marks_detected_effects_and_drops_ones_the_worker_noted(
    db: sqlite3.Connection,
) -> None:
    run = make_run(db)
    record_detected(db, run.code, classify_command("gh pr merge 3", CTX)[0])
    record_detected(db, run.code, classify_command("git push origin main", CTX)[0])
    assert shown_did(db, run.code) == [
        "merged PR #3 with gh (detected)",
        "pushed to main (detected)",
    ]
    record_did(db, run.code, "merged PR #3 in dori with gh")  # the worker's own note
    assert shown_did(db, run.code) == ["merged PR #3 in dori with gh", "pushed to main (detected)"]
    line = reports(db).running(run, "<30m")
    assert line.title.endswith("· did: merged PR #3 in dori with gh; pushed to main (detected)")
    end = reports(db).ended(replace(run, status="failed", failure="boom"), "R1 failed: boom",
                            urgent=True)  # fmt: skip
    assert (
        end.title
        == "R1 failed: boom · did: merged PR #3 in dori with gh; pushed to main (detected)"
    )
    assert reports(db).did_line(run) == (
        "Did: merged PR #3 in dori with gh; pushed to main (detected)"
    )


def test_a_high_impact_effect_texts_now_unless_a_rule_holds_it(db: sqlite3.Connection) -> None:
    run = make_run(db)
    r = reports(db)
    assert r.level(run, "side_effect", "push") == "now"
    assert r.level(run, "side_effect", "publish") == "now"
    trust_store.add_rule(db, scope="proj", action="push", effect="notify_digest",
                         created_from="chat", now=NOW)  # fmt: skip
    assert r.level(run, "side_effect", "push") == "digest"
    assert r.level(run, "side_effect", "publish") == "now"
    texts = [r.detected_text(run, "pushed to main") for _ in range(NOTE_TEXTS_PER_HOUR + 1)]
    assert texts[0] == "R1 detected: pushed to main"
    assert texts[-1] is None  # the per-run cap it shares with urgent notes


# --- the runner, with the fake claude ---------------------------------------------------------


async def test_the_runner_reads_side_effects_from_the_stream(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    secret = "ghp_" + "Z" * 36
    h = build(
        tmp_path,
        db,
        extra=[
            "--bash", "git status",
            "--bash", "git push -u origin HEAD",  # its own branch: nothing
            "--bash", f"curl -X POST -H 'Authorization: token {secret}' https://hooks.example.com/x",
            "--bash", "git push origin main",
            "--bash", "git push origin main",  # again: listed and texted once
        ],
    )  # fmt: skip
    code = await h.start(report="digest")
    await h.drain()
    assert h.run(code).status == "ready"
    assert [e.what for e in detected_list(db, code)] == [
        "POST hooks.example.com (curl)",
        "pushed to main",
    ]
    assert h.chan.texts() == [f"{code} detected: pushed to main"]  # the high-impact one only
    [line] = pulse_store.pending(db, ["normal", "urgent"], utcnow())
    assert line.title.endswith(
        "· did: POST hooks.example.com (curl) (detected); pushed to main (detected)"
    )
    rows = db.execute("SELECT detail FROM audit_log WHERE action = 'run.side_effect'").fetchall()
    assert len(rows) == 2
    assert secret not in line.title and all(secret not in str(r[0]) for r in rows)


async def test_a_noted_side_effect_is_not_listed_twice(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(
        tmp_path, db,
        extra=["--bash", "gh pr merge 3 -R own/other", "--note", "did: merged PR #3 in other"],
    )  # fmt: skip
    code = await h.start(report="digest")
    await h.drain()
    assert [e.what for e in detected_list(db, code)] == ["merged PR #3 with gh in own/other"]
    assert h.chan.texts() == []  # a merge isn't high impact
    [line] = pulse_store.pending(db, ["normal", "urgent"], utcnow())
    assert line.title.endswith("· did: merged PR #3 in other")


async def test_a_notify_digest_rule_keeps_a_detected_push_out_of_texts(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    trust_store.add_rule(db, scope="proj", action="push", effect="notify_digest",
                         created_from="chat", now=NOW)  # fmt: skip
    h = build(tmp_path, db, extra=["--bash", "git push origin main"])
    code = await h.start(report="digest")
    await h.drain()
    assert h.chan.texts() == []
    [line] = pulse_store.pending(db, ["normal", "urgent"], utcnow())
    assert line.title.endswith("· did: pushed to main (detected)")
    assert code == "R1"
