"""Local-only sandbox probes: need the Mini (Keychain, launchd, HID, LAN, claude). Skipped in CI."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator

import pytest

from resonant.sandbox.probes import PROBES, ProbeContext, probe_context, run_probes

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or bool(os.environ.get("CI")),
    reason="local-only: runs on the Mini, not in CI",
)


@pytest.fixture(scope="module")
def ctx(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ProbeContext]:
    # PER-271: the real home (these probes need its Keychain and launchd), but the run dirs,
    # cache and secrets in a temp dir: never the live ~/resonant-runs or ~/.resonant
    state = tmp_path_factory.mktemp("probe-state")
    with probe_context(work_dir=state / "resonant-runs", secrets_dir=state / "run-secrets") as c:
        yield c


def test_local_probes_pass(ctx: ProbeContext) -> None:
    results = run_probes(ctx, ("local",))
    assert len(results) == sum(p.scope == "local" for p in PROBES)
    failed = [f"{r.name}: {r.detail}" for r in results if r.status == "fail"]
    assert not failed
