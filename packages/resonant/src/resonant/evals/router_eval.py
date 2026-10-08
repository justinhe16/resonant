"""Router eval: routing quality and speed on the owner's own texts.

``resonant eval router --set PATH [--repeat N] [--json] [--min-accuracy 0.9]``

- **Input.** JSONL, one ``{"text": str, "intent": <IntentLabel>, "principal"?: "owner"}`` per
  line (blank lines are skipped). The default set is ``~/.resonant/evals/router.jsonl``,
  outside the public repo; ``evals/router.example.jsonl`` is a synthetic example.
- **Run.** Every text goes through ``resonant.router.router.classify``: the production
  fast-path matcher and labeler, not a copy. It runs no tools and never touches the kill
  switch. When any text reaches the labeler, one untimed warm-up call is made first so a
  cold model load doesn't skew the latencies. ``--repeat N`` runs the whole set N times.
- **Output.** Accuracy, per-intent precision and recall, the confusion matrix, p50/p95
  latency overall and per path (``fast`` | ``llm``), fast-path coverage, labeler fallbacks
  and the misclassified examples.
- **Texts are PII.** They appear only in what the CLI prints. They are never logged, and the
  summary saved to ``~/.resonant/evals/results/<ts>.json`` for trend tracking refers to
  examples by line number only. Load errors name the line, never the text.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import Counter
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from resonant.config import Settings
from resonant.models import ModelClient
from resonant.models.bench import percentile
from resonant.router import router as _router
from resonant.router.fast_path import match_fast_path
from resonant.router.intents import INTENTS, IntentLabel, parse_intent
from resonant.router.labeler import Labeler
from resonant.router.prefixes import LABEL_PREFIX_HASH

log = logging.getLogger(__name__)

DEFAULT_MIN_ACCURACY = 0.9
WARMUP_TEXT = "hello, are you there?"  # synthetic: never one of the owner's texts

EXIT_OK = 0
EXIT_BELOW_MIN = 1
EXIT_BAD_SET = 2

RoutePath = Literal["fast", "llm"]
PATHS: tuple[RoutePath, ...] = ("fast", "llm")


def default_set_path(settings: Settings) -> Path:
    return settings.home / "evals" / "router.jsonl"


def results_dir(settings: Settings) -> Path:
    return settings.home / "evals" / "results"


# --- loading ---------------------------------------------------------------------------


class EvalSetError(ValueError):
    """The eval set is missing or malformed. The message never contains a text."""


@dataclass(frozen=True)
class EvalCase:
    line: int  # 1-based line number in the set: how saved results refer to the example
    text: str
    intent: IntentLabel


def load_set(path: Path) -> list[EvalCase]:
    """Parse a JSONL eval set. Raises ``EvalSetError`` naming the line (never the text)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise EvalSetError(f"{path}: not found") from None
    except (OSError, UnicodeDecodeError) as e:
        raise EvalSetError(f"{path}: cannot read ({type(e).__name__})") from None
    cases: list[EvalCase] = []
    for n, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj: Any = json.loads(line)
        except json.JSONDecodeError:
            raise EvalSetError(f"{path}:{n}: not valid JSON") from None
        if not isinstance(obj, dict):
            raise EvalSetError(f"{path}:{n}: expected a JSON object")
        # json.loads returns Any; the isinstance check above makes this a JSON object.
        row: dict[str, Any] = obj  # pyright: ignore[reportUnknownVariableType]
        text = row.get("text")
        if not isinstance(text, str) or not text.strip():
            raise EvalSetError(f"{path}:{n}: 'text' must be a non-empty string")
        intent = parse_intent(row.get("intent"))
        if intent is None:
            raise EvalSetError(f"{path}:{n}: 'intent' must be one of {', '.join(INTENTS)}")
        # Validated but not passed on: classify routes from the owner's point of view.
        if row.get("principal", "owner") != "owner":
            raise EvalSetError(f"{path}:{n}: only principal 'owner' is supported")
        cases.append(EvalCase(n, text, intent))
    if not cases:
        raise EvalSetError(f"{path}: no examples")
    return cases


def set_digest(path: Path) -> str:
    """Short sha256 of the set file, so saved results show when the set changed."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


# --- running ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseResult:
    line: int
    expected: IntentLabel
    predicted: IntentLabel
    path: RoutePath
    latency_ms: float
    fallback: bool

    @property
    def correct(self) -> bool:
        return self.expected == self.predicted


async def run_eval(
    cases: Sequence[EvalCase], labeler: Labeler, *, repeat: int = 1
) -> list[CaseResult]:
    """Classify every case ``repeat`` times through the production ``classify``."""
    if repeat < 1:
        raise ValueError("repeat must be >= 1")
    if any(match_fast_path(c.text) is None for c in cases):
        await labeler.label(WARMUP_TEXT)  # untimed: absorbs a cold model load
    results: list[CaseResult] = []
    for _ in range(repeat):
        for case in cases:
            start = time.perf_counter()
            # Looked up on the module at call time: this is the production entry point.
            c = await _router.classify(case.text, labeler)
            latency_ms = (time.perf_counter() - start) * 1000
            results.append(
                CaseResult(case.line, case.intent, c.intent, c.path, latency_ms, c.fallback)
            )
    return results


# --- metrics ---------------------------------------------------------------------------


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _latency(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "p50_ms": None, "p95_ms": None}
    return {
        "n": len(values),
        "p50_ms": round(percentile(values, 50), 1),
        "p95_ms": round(percentile(values, 95), 1),
    }


def summarize(results: Sequence[CaseResult]) -> dict[str, Any]:
    """All metrics, keyed for JSON. Contains no texts: examples are referred to by line."""
    n = len(results)
    correct = sum(r.correct for r in results)
    expected = Counter(r.expected for r in results)
    predicted = Counter(r.predicted for r in results)
    hits = Counter(r.expected for r in results if r.correct)
    per_intent = {
        i: {
            "support": expected[i],
            "predicted": predicted[i],
            "precision": _ratio(hits[i], predicted[i]),
            "recall": _ratio(hits[i], expected[i]),
        }
        for i in INTENTS
    }
    labels = [i for i in INTENTS if expected[i] or predicted[i]]
    pairs = Counter((r.expected, r.predicted) for r in results)
    confusion = {e: {p: pairs[(e, p)] for p in labels} for e in labels}
    by_path = {p: [r.latency_ms for r in results if r.path == p] for p in PATHS}
    wrong = Counter((r.line, r.expected, r.predicted, r.path) for r in results if not r.correct)
    misclassified = [
        {"line": line, "expected": e, "predicted": p, "path": path, "count": count}
        for (line, e, p, path), count in sorted(wrong.items())
    ]
    return {
        "n": n,
        "correct": correct,
        "accuracy": _ratio(correct, n),
        "per_intent": per_intent,
        "confusion": confusion,
        "latency": {
            "overall": _latency([r.latency_ms for r in results]),
            **{p: _latency(v) for p, v in by_path.items()},
        },
        "fast_path_coverage": _ratio(len(by_path["fast"]), n),
        "fallbacks": sum(r.fallback for r in results),
        "misclassified": misclassified,
    }


# --- rendering (terminal only: the one place texts appear) ------------------------------


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v * 100:.1f}%"


def _ms(v: float | None) -> str:
    return "-" if v is None else f"{v:.1f}"


def render_text(summary: dict[str, Any], texts: dict[int, str]) -> str:
    """Human-readable report. Misclassified examples include their text."""
    out: list[str] = []
    meta = summary["meta"]
    out.append(
        f"router eval  {meta['set']}  ({meta['cases']} examples x {meta['repeat']}, "
        f"model {meta['model']})"
    )
    passed = "ok" if meta["passed"] else "BELOW TARGET"
    out.append(
        f"accuracy     {_pct(summary['accuracy'])} ({summary['correct']}/{summary['n']})"
        f"   target {_pct(meta['min_accuracy'])}  {passed}"
    )
    out.append(f"fast path    {_pct(summary['fast_path_coverage'])} of messages")
    if summary["fallbacks"]:
        out.append(
            f"fallbacks    {summary['fallbacks']} labels fell back (model unavailable?"
            " try `resonant model probe`)"
        )
    out.append("")
    out.append("latency (ms)      n      p50      p95")
    for key in ("overall", *PATHS):
        lat = summary["latency"][key]
        out.append(f"  {key:<10} {lat['n']:>6} {_ms(lat['p50_ms']):>8} {_ms(lat['p95_ms']):>8}")
    out.append("")
    out.append("intent          support  precision   recall")
    for intent, m in summary["per_intent"].items():
        if m["support"] or m["predicted"]:
            out.append(
                f"  {intent:<14}{m['support']:>7} {_pct(m['precision']):>10} {_pct(m['recall']):>8}"
            )
    out.append("")
    labels = list(summary["confusion"])
    width = max(len(x) for x in labels) + 2
    out.append("confusion (rows: expected, columns: predicted)")
    out.append(" " * width + "".join(f"{p[:11]:>12}" for p in labels))
    for e, row in summary["confusion"].items():
        out.append(f"{e:<{width}}" + "".join(f"{row[p]:>12}" for p in labels))
    out.append("")
    wrong = summary["misclassified"]
    out.append(f"misclassified ({len(wrong)})")
    for m in wrong:
        times = f" x{m['count']}" if m["count"] > 1 else ""
        out.append(
            f"  line {m['line']:>4}  {m['expected']} -> {m['predicted']} ({m['path']}){times}"
            f"  {texts[m['line']]!r}"
        )
    if meta.get("saved_to"):
        out.append("")
        out.append(f"summary saved to {meta['saved_to']} (no texts)")
    return "\n".join(out)


def with_texts(summary: dict[str, Any], texts: dict[int, str]) -> dict[str, Any]:
    """A copy for terminal JSON output, with each misclassified example's text added."""
    wrong = [{**m, "text": texts[m["line"]]} for m in summary["misclassified"]]
    return {**summary, "misclassified": wrong}


# --- saving ----------------------------------------------------------------------------


def save_summary(summary: dict[str, Any], directory: Path, *, now: datetime) -> Path:
    """Write the text-free summary to ``<directory>/<ts>.json`` and return the path."""
    directory.mkdir(parents=True, exist_ok=True)
    stem = now.strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"{stem}.json"
    k = 1
    while path.exists():
        path = directory / f"{stem}-{k}.json"
        k += 1
    path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return path


# --- CLI entry -------------------------------------------------------------------------


def open_model(settings: Settings) -> AbstractAsyncContextManager[ModelClient]:
    """The production model client. Tests replace this with a fake."""
    from resonant.models import OllamaClient  # lazy: the openai SDK is slow to import

    return OllamaClient(settings.model)


def run(
    settings: Settings,
    *,
    set_path: Path | None,
    repeat: int,
    as_json: bool,
    min_accuracy: float,
    echo: Callable[[str], None],
    echo_err: Callable[[str], None],
) -> int:
    """Run the eval, print the report, save the text-free summary. Returns the exit code."""
    path = (set_path or default_set_path(settings)).expanduser()
    try:
        cases = load_set(path)
        digest = set_digest(path)
    except EvalSetError as e:
        echo_err(f"error: {e}")
        if set_path is None:
            echo_err("build your set first: see evals/README.md")
        return EXIT_BAD_SET

    async def go() -> list[CaseResult]:
        async with open_model(settings) as model:
            return await run_eval(cases, Labeler(model), repeat=repeat)

    now = datetime.now(UTC)
    results = asyncio.run(go())
    summary = summarize(results)
    accuracy = summary["accuracy"] or 0.0
    passed = accuracy >= min_accuracy
    summary = {
        "meta": {
            "timestamp": now.isoformat(timespec="seconds"),
            "set": str(path),
            "set_sha256": digest,
            "cases": len(cases),
            "repeat": repeat,
            "model": settings.model.name,
            "label_prefix_hash": LABEL_PREFIX_HASH,
            "min_accuracy": min_accuracy,
            "passed": passed,
        },
        **summary,
    }
    saved = save_summary(summary, results_dir(settings), now=now)
    log.info(
        "router eval: %d results, accuracy %s, saved %s", summary["n"], summary["accuracy"], saved
    )
    summary["meta"]["saved_to"] = str(saved)
    texts = {c.line: c.text for c in cases}
    if as_json:
        echo(json.dumps(with_texts(summary, texts), indent=2))
    else:
        echo(render_text(summary, texts))
    return EXIT_OK if passed else EXIT_BELOW_MIN
