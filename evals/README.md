# evals

The example sets here are synthetic. Your real sets, built from your own texts, live outside the public repo in `~/.resonant/evals/` and are passed with `--set`.

- `router.example.jsonl`: `{text, intent}` pairs for the router. Run with `resonant eval router`.
- `approvals.example.jsonl`: replies to approval requests covering yes/no/ambiguous/sarcasm/unrelated/pay, with the expected decision and which path (`parser` or `classifier`) should decide (Phase 2).

## Router eval

```sh
resonant eval router [--set PATH] [--repeat N] [--json] [--min-accuracy 0.9]
```

Each text goes through the production router (`resonant.router.classify`): the deterministic fast path first, then one labeler call to the local model. It runs no tools and never touches the kill switch. The command prints:

- accuracy, with the target (`--min-accuracy`, default `0.9`)
- precision and recall per intent
- the confusion matrix (rows are expected intents, columns are predicted)
- p50/p95 latency in ms, overall and per path (`fast` = no model call, `llm` = labeled)
- fast-path coverage: the share of messages that never reach the model
- labeler fallbacks (the model was unavailable; run `resonant model probe`)
- the misclassified examples, with their line number and text

Exit codes: `0` when accuracy ≥ `--min-accuracy`, `1` when it's below, `2` when the set is missing or malformed. `--repeat N` runs the whole set N times, which gives steadier latencies and shows whether labels are stable. `--json` prints the same report as JSON.

When any text reaches the labeler, one untimed warm-up call with a synthetic text goes first, so a cold model load doesn't skew the latencies.

### Where texts go

Texts appear **only in the command's own output** (the misclassified examples, in both the text and `--json` output). They are never logged. If you redirect `--json` output to a file, that file contains the misclassified texts.

Every run also saves a summary to `~/.resonant/evals/results/<timestamp>.json` for trend tracking. It contains the metrics, the model name, the labeler's prompt-prefix hash, a hash of the set file and the misclassified examples **by line number only**, never their text. Compare these files over time to see how a prompt or model change moved the numbers.

### Building your real set

The default set is `~/.resonant/evals/router.jsonl`. It stays outside the repo and is never committed.

1. Collect 50–200 messages you actually send or would send Resonant, from iMessage history or written fresh. Anonymise them as you like, but keep the wording natural (typos, shorthand, emoji).
2. Write one JSON object per line:

   ```json
   {"text": "anything stuck waiting on me?", "intent": "job_control"}
   ```

   - `text`: the message, exactly as you'd send it.
   - `intent`: what the router *should* say. One of `question`, `job_control`, `system`, `run_project`, `unknown` (see `resonant/router/intents.py` for the definitions).
   - `principal` (optional): only `"owner"` is supported. The eval routes from the owner's point of view.

   Blank lines are skipped. A malformed line fails the load with its line number (the text is never echoed).
3. Cover every intent, with at least ~10 examples each. Include the fast-path phrasings (`status`, `ping`, `what's running`, `how's the mini`, `/status`, `/tasks`, `/help`) and near-misses that should *not* hit the fast path ("what's running on the build box?").
4. Don't copy the labeler's few-shot examples (`resonant/router/prefixes.py`) into the set. That inflates accuracy.
5. Label by what you meant, not by what the router currently says. Disagreements are the point.

### Target

Accuracy **≥ 90%** on your real set before going live (`docs/go-live.md`, step 9):

```sh
resonant eval router --set ~/.resonant/evals/router.jsonl --repeat 3
```

If it's lower, read the misclassified examples and the confusion matrix first. A gap in one intent usually means the intent definitions or few-shot examples in `prefixes.py` need work. After changing a prompt or model, rerun and compare with the previous summary in `~/.resonant/evals/results/`.

The synthetic `router.example.jsonl` only proves the pipeline works (tests run it with a fake model). Its accuracy against a real model says little about your own texts.
