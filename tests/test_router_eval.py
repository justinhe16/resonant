from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest
from typer.testing import CliRunner

from resonant.cli import app
from resonant.config import Settings, load_settings
from resonant.evals import router_eval
from resonant.evals.router_eval import (
    CaseResult,
    EvalCase,
    EvalSetError,
    load_set,
    run_eval,
    summarize,
)
from resonant.models import (
    ChatMessage,
    ModelReply,
    ModelUnavailableError,
    ProbeResult,
    SpanFactory,
)
from resonant.router import Labeler, match_fast_path
from resonant.router import router as router_mod
from resonant.router.intents import BUILTIN_INTENTS, IntentLabel, IntentSet
from resonant_sdk import Manifest
from resonant_sdk.manifest import parse_yaml

EXAMPLE_SET = Path(__file__).resolve().parents[1] / "evals" / "router.example.jsonl"
_MESSAGE = re.compile(r"\A<message>(.*)</message>\Z", re.DOTALL)


def _example_labels() -> dict[str, IntentLabel]:
    return {c.text: c.intent for c in load_set(EXAMPLE_SET)}


class FakeModel:
    """Labels each text from a lookup table; anything else gets ``default``."""

    def __init__(
        self,
        labels: dict[str, IntentLabel] | None = None,
        *,
        default: IntentLabel = "unknown",
        error: Exception | None = None,
    ) -> None:
        self.labels = labels or {}
        self.default = default
        self.error = error
        self.texts: list[str] = []

    async def chat_json(
        self,
        messages: list[ChatMessage],
        *,
        schema: dict[str, Any],
        max_tokens: int = 512,
        temperature: float = 0.0,
        spans: SpanFactory | None = None,
    ) -> ModelReply:
        m = _MESSAGE.match(messages[-1]["content"])
        assert m is not None
        text = m.group(1)
        self.texts.append(text)
        if self.error is not None:
            raise self.error
        data = {"intent": self.labels.get(text, self.default), "confidence": 0.9}
        return ModelReply(json.dumps(data), [], data, 3, 10, 3)

    async def chat_tools(self, messages: list[ChatMessage], **kw: Any) -> ModelReply:
        raise AssertionError("the router eval never calls chat_tools")

    async def probe(self) -> ProbeResult:
        raise AssertionError("the router eval never probes")

    async def __aenter__(self) -> FakeModel:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


@pytest.fixture
def use_model(monkeypatch: pytest.MonkeyPatch) -> Any:
    def install(model: FakeModel) -> FakeModel:
        def open_model(settings: Settings) -> FakeModel:
            return model

        monkeypatch.setattr(router_eval, "open_model", open_model)
        return model

    return install


def _invoke(*args: str) -> Any:
    return CliRunner().invoke(app, ["eval", "router", *args])


def _saved(home: Path) -> list[Path]:
    return sorted((home / "evals" / "results").glob("*.json"))


# --- loading -----------------------------------------------------------------------------


def test_example_set_is_valid_and_covers_both_paths() -> None:
    cases = load_set(EXAMPLE_SET)
    assert len(cases) >= 20
    assert {c.intent for c in cases} == {
        "question",
        "job_control",
        "system",
        "notes",
        "reminders",
        "ops",
        "run_project",
        "unknown",
    }
    fast = [c for c in cases if match_fast_path(c.text) is not None]
    assert 0 < len(fast) < len(cases)
    for c in fast:  # the synthetic labels agree with the deterministic fast path
        cmd = match_fast_path(c.text)
        assert cmd is not None and cmd.intent == c.intent


@pytest.mark.parametrize(
    ("line", "error"),
    [
        ("not json secret-text", "not valid JSON"),
        ('["secret-text"]', "expected a JSON object"),
        ('{"intent": "question"}', "'text' must be"),
        ('{"text": "   ", "intent": "question"}', "'text' must be"),
        ('{"text": "secret-text", "intent": "extension:x"}', "'intent' must be one of"),
        ('{"text": "secret-text", "intent": "question", "principal": "kid"}', "only principal"),
    ],
)
def test_load_set_errors_name_the_line_never_the_text(
    tmp_path: Path, line: str, error: str
) -> None:
    path = tmp_path / "set.jsonl"
    path.write_text('{"text": "ok", "intent": "question"}\n\n' + line + "\n")
    with pytest.raises(EvalSetError) as e:
        load_set(path)
    assert f"{path}:3:" in str(e.value)
    assert error in str(e.value)
    assert "secret-text" not in str(e.value)


def test_load_set_missing_and_empty(tmp_path: Path) -> None:
    with pytest.raises(EvalSetError, match="not found"):
        load_set(tmp_path / "nope.jsonl")
    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n\n")
    with pytest.raises(EvalSetError, match="no examples"):
        load_set(empty)


def test_load_set_accepts_owner_principal_and_line_numbers(tmp_path: Path) -> None:
    path = tmp_path / "set.jsonl"
    path.write_text(
        '\n{"text": "a", "intent": "question", "principal": "owner"}\n'
        '{"text": "b", "intent": "system", "note": "extra keys are fine"}\n'
    )
    assert load_set(path) == [EvalCase(2, "a", "question"), EvalCase(3, "b", "system")]


# --- metrics -----------------------------------------------------------------------------


def test_summarize_metrics() -> None:
    results = [
        CaseResult(1, "system", "system", "fast", 1.0, False),
        CaseResult(2, "system", "job_control", "llm", 10.0, False),
        CaseResult(3, "job_control", "job_control", "llm", 20.0, False),
        CaseResult(4, "question", "unknown", "llm", 30.0, True),
    ]
    s = summarize(results)
    assert s["n"] == 4
    assert s["correct"] == 2
    assert s["accuracy"] == 0.5
    assert s["per_intent"]["system"] == {
        "support": 2,
        "predicted": 1,
        "precision": 1.0,
        "recall": 0.5,
    }
    assert s["per_intent"]["job_control"]["precision"] == 0.5
    assert s["per_intent"]["job_control"]["recall"] == 1.0
    assert s["per_intent"]["question"]["precision"] is None  # never predicted
    assert s["per_intent"]["run_project"] == {
        "support": 0,
        "predicted": 0,
        "precision": None,
        "recall": None,
    }
    assert list(s["confusion"]) == ["question", "job_control", "system", "unknown"]
    assert s["confusion"]["system"] == {
        "question": 0,
        "job_control": 1,
        "system": 1,
        "unknown": 0,
    }
    assert s["fast_path_coverage"] == 0.25
    assert s["fallbacks"] == 1
    assert s["latency"]["overall"] == {"n": 4, "p50_ms": 10.0, "p95_ms": 30.0}
    assert s["latency"]["fast"] == {"n": 1, "p50_ms": 1.0, "p95_ms": 1.0}
    assert s["latency"]["llm"] == {"n": 3, "p50_ms": 20.0, "p95_ms": 30.0}
    assert s["misclassified"] == [
        {"line": 2, "expected": "system", "predicted": "job_control", "path": "llm", "count": 1},
        {"line": 4, "expected": "question", "predicted": "unknown", "path": "llm", "count": 1},
    ]
    assert "text" not in json.dumps(s)


# --- running -----------------------------------------------------------------------------


async def test_eval_calls_the_production_classify(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real = router_mod.classify

    async def spy(text: str, labeler: Labeler, *, spans: SpanFactory | None = None) -> Any:
        calls.append(text)
        return await real(text, labeler, spans=spans)

    monkeypatch.setattr(router_mod, "classify", spy)
    cases = load_set(EXAMPLE_SET)
    model = FakeModel(_example_labels())
    results = await run_eval(cases, Labeler(model), repeat=2)
    assert calls == [c.text for c in cases] * 2
    assert len(results) == 2 * len(cases)
    assert all(r.correct for r in results)
    # The fast path never reaches the model; one warm-up call comes first.
    llm = [c.text for c in cases if match_fast_path(c.text) is None]
    assert model.texts == [router_eval.WARMUP_TEXT, *llm, *llm]
    assert {r.path for r in results} == {"fast", "llm"}


async def test_no_warmup_when_everything_is_fast_path() -> None:
    model = FakeModel()
    results = await run_eval([EvalCase(1, "status", "system")], Labeler(model))
    assert model.texts == []
    assert results[0].path == "fast"


async def test_repeat_must_be_positive() -> None:
    with pytest.raises(ValueError, match="repeat"):
        await run_eval([EvalCase(1, "status", "system")], Labeler(FakeModel()), repeat=0)


# --- CLI ---------------------------------------------------------------------------------


def test_cli_example_set_prints_all_metrics(use_model: Any, resonant_home: Path) -> None:
    use_model(FakeModel(_example_labels()))
    result = _invoke("--set", str(EXAMPLE_SET))
    assert result.exit_code == 0, result.output
    out = result.output
    for needle in (
        "accuracy     100.0%",
        "fast path",
        "latency (ms)",
        "overall",
        "fast",
        "llm",
        "precision",
        "recall",
        "confusion",
        "misclassified (0)",
        "summary saved to",
    ):
        assert needle in out
    assert len(_saved(resonant_home)) == 1


def test_cli_json_output(use_model: Any, resonant_home: Path) -> None:
    labels = _example_labels()
    labels["asdfgh"] = "question"  # one wrong answer
    use_model(FakeModel(labels))
    result = _invoke("--set", str(EXAMPLE_SET), "--json", "--repeat", "2")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["meta"]["repeat"] == 2
    assert data["meta"]["cases"] == len(labels)
    assert data["meta"]["passed"] is True
    assert data["n"] == 2 * len(labels)
    assert set(data["latency"]) == {"overall", "fast", "llm"}
    assert data["fast_path_coverage"] is not None
    assert data["misclassified"] == [
        {
            "line": 23,
            "expected": "unknown",
            "predicted": "question",
            "path": "llm",
            "count": 2,
            "text": "asdfgh",
        }
    ]
    assert data["meta"]["saved_to"] == str(_saved(resonant_home)[0])


def test_texts_never_reach_logs_or_saved_results(
    use_model: Any, resonant_home: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    texts = ["zebra-alpha private words", "zebra-beta private words", "status"]
    path = tmp_path / "set.jsonl"
    path.write_text("".join(json.dumps({"text": t, "intent": "question"}) + "\n" for t in texts))
    use_model(FakeModel(default="system"))  # everything is wrong
    caplog.set_level(logging.DEBUG)
    result = _invoke("--set", str(path), "--min-accuracy", "0")
    assert result.exit_code == 0, result.output
    assert "zebra-alpha private words" in result.output  # terminal output only
    saved = _saved(resonant_home)
    assert len(saved) == 1
    content = saved[0].read_text()
    data = json.loads(content)
    assert [m["line"] for m in data["misclassified"]] == [1, 2, 3]
    assert "saved_to" not in data["meta"]
    for t in ("zebra", "private words"):
        assert t not in content
        assert t not in caplog.text
        for record in caplog.records:
            assert t not in record.getMessage()


def test_min_accuracy_exit_code(use_model: Any) -> None:
    labels = _example_labels()
    use_model(FakeModel(labels, default="unknown"))
    assert _invoke("--set", str(EXAMPLE_SET)).exit_code == 0
    use_model(FakeModel(default="question"))  # only fast path + question texts are right
    below = _invoke("--set", str(EXAMPLE_SET))
    assert below.exit_code == 1
    assert "BELOW TARGET" in below.output
    assert _invoke("--set", str(EXAMPLE_SET), "--min-accuracy", "0.2").exit_code == 0
    assert _invoke("--set", str(EXAMPLE_SET), "--min-accuracy", "1.5").exit_code == 2


def test_default_set_missing_exits_2(use_model: Any, resonant_home: Path) -> None:
    use_model(FakeModel())
    result = _invoke()
    assert result.exit_code == 2
    assert str(resonant_home / "evals" / "router.jsonl") in result.output
    assert "evals/README.md" in result.output
    assert _saved(resonant_home) == []


def test_default_set_is_used(use_model: Any, resonant_home: Path) -> None:
    (resonant_home / "evals").mkdir()
    (resonant_home / "evals" / "router.jsonl").write_text('{"text": "ping", "intent": "system"}\n')
    use_model(FakeModel())
    result = _invoke("--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["accuracy"] == 1.0
    assert data["fast_path_coverage"] == 1.0


def test_model_unavailable_counts_fallbacks(use_model: Any) -> None:
    use_model(FakeModel(error=ModelUnavailableError("down")))
    result = _invoke("--set", str(EXAMPLE_SET))
    assert result.exit_code == 1
    assert "fell back" in result.output
    assert "resonant model probe" in result.output


def test_saved_results_do_not_collide(resonant_home: Path) -> None:
    from datetime import UTC, datetime

    now = datetime(2026, 10, 8, 12, 0, 0, tzinfo=UTC)
    d = resonant_home / "evals" / "results"
    a = router_eval.save_summary({"n": 1}, d, now=now)
    b = router_eval.save_summary({"n": 2}, d, now=now)
    assert a.name == "20261008T120000Z.json"
    assert b.name == "20261008T120000Z-1.json"


# --- extension intents (P3-11) -------------------------------------------------------------

DEMO = """\
name: demo
version: 1
description: Demo things; tells the time in a timezone.
examples:
  - what time is it in tokyo
  - time in paris please
tools:
  get_time:
    run: ./cli.py time
    effect: read
"""


def _demo() -> Manifest:
    return Manifest.model_validate(parse_yaml(DEMO))


def test_load_set_takes_extension_intents_only_when_installed(tmp_path: Path) -> None:
    path = tmp_path / "set.jsonl"
    path.write_text('{"text": "time in tokyo", "intent": "extension:demo"}\n')
    with pytest.raises(EvalSetError, match="must be one of"):
        load_set(path)
    (case,) = load_set(path, IntentSet.from_manifests([_demo()]))
    assert case.intent == "extension:demo"


def test_summarize_reports_extension_intents() -> None:
    results = [
        CaseResult(1, "extension:demo", "extension:demo", "llm", 5.0, False),
        CaseResult(2, "notes", "extension:demo", "llm", 5.0, False),
    ]
    s = summarize(results)
    assert s["per_intent"]["extension:demo"]["precision"] == 0.5
    assert list(s["confusion"]) == ["notes", "extension:demo"]


def test_installed_intents_come_from_enabled_rows(db: Any, resonant_home: Path) -> None:
    from resonant.e2e.extensions import install_fixture

    settings = load_settings()
    install_fixture(settings, db, DEMO, {"cli.py": "print('{}')\n"})
    assert [e.label for e in router_eval.installed_intents(db).extensions] == ["extension:demo"]
    assert router_eval.installed_intents(db, exclude="demo") == BUILTIN_INTENTS


def test_advisory_prints_builtin_accuracy_before_and_after(use_model: Any) -> None:
    labels = _example_labels()
    # After the new intent exists, the model sends one builtin text to it.
    stolen = next(t for t, i in labels.items() if i == "notes" and not match_fast_path(t))

    class Stealing(FakeModel):
        async def chat_json(self, messages: list[ChatMessage], **kw: Any) -> ModelReply:
            reply = await super().chat_json(messages, **kw)
            enum = kw["schema"]["properties"]["intent"]["enum"]
            if "extension:demo" in enum and self.texts[-1] == stolen:
                data = {"intent": "extension:demo", "confidence": 0.9}
                return ModelReply(json.dumps(data), [], data, 3, 10, 3)
            return reply

    demo_texts = {t: "extension:demo" for t in _demo().examples}
    use_model(Stealing({**labels, **demo_texts}))
    lines = router_eval.advisory(
        load_settings(), _demo(), before=BUILTIN_INTENTS, set_path=EXAMPLE_SET
    )
    builtin = len(load_set(EXAMPLE_SET))
    assert lines[0] == (
        f"Router: builtin accuracy 100.0% before, {(builtin - 1) / builtin * 100:.1f}% after "
        f"({builtin} examples, advisory)"
    )
    assert lines[1].startswith("warning: extension:demo takes messages from builtin intents")
    assert lines[2] == "Router: 2/2 of its examples route to extension:demo"


def test_advisory_never_blocks(use_model: Any, tmp_path: Path) -> None:
    settings = load_settings()
    use_model(FakeModel(error=ModelUnavailableError("down")))
    assert router_eval.advisory(
        settings, _demo(), before=BUILTIN_INTENTS, set_path=EXAMPLE_SET
    ) == ["Router: advisory eval skipped (model unavailable; try resonant model probe)"]
    missing = tmp_path / "nope.jsonl"
    assert router_eval.advisory(settings, _demo(), before=BUILTIN_INTENTS, set_path=missing) == [
        "Router: advisory eval skipped (no example set)"
    ]
    no_description = Manifest.model_validate(parse_yaml(DEMO.replace("description:", "#")))
    assert router_eval.advisory(settings, no_description, before=BUILTIN_INTENTS) == []
