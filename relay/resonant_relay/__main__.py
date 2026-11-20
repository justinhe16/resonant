"""Entry point: `python -m resonant_relay`. Reads settings from the environment, starts the
checker thread and serves the dashboard until SIGTERM/SIGINT."""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Iterable, Mapping
from types import FrameType

from resonant_relay import config, web
from resonant_relay.alerts import Notifier, NtfyNotifier, SlackWebhookNotifier, WebhookNotifier
from resonant_relay.checker import Checker
from resonant_relay.source import RpcSource, StatusSource, TableSource

log = logging.getLogger("resonant_relay")


class RedactFilter(logging.Filter):
    """Replaces every configured secret in a formatted log line with [redacted]. Code never
    logs secrets on purpose; this is the second line behind that."""

    def __init__(self, secrets: Iterable[str]):
        super().__init__()
        self._secrets = sorted({s for s in secrets if len(s) >= 4}, key=len, reverse=True)

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for s in self._secrets:
            msg = msg.replace(s, "[redacted]")
        record.msg, record.args = msg, None
        if record.exc_info:
            record.exc_text = None
            record.exc_info = None  # tracebacks may hold URLs; the message is enough
        return True


def setup_logging(secrets: Iterable[str]) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactFilter(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)


def build_source(s: config.Settings) -> StatusSource:
    if s.read_mode == "table":
        return TableSource(s.supabase_url, s.read_key, s.instances)
    return RpcSource(s.supabase_url, s.anon_key, s.status_token)


def build_notifiers(s: config.Settings) -> list[Notifier]:
    """The global channels (ntfy, webhook), then one Slack channel per instance that routes
    to its own Slack webhook (`slack:<instance>`)."""
    out: list[Notifier] = []
    if s.ntfy_topic:
        out.append(NtfyNotifier(s.ntfy_url, s.ntfy_topic, s.ntfy_token))
    if s.webhook_url:
        out.append(WebhookNotifier(s.webhook_url))
    for inst in s.instances:
        if "slack" in s.channels_for(inst):
            out.append(SlackWebhookNotifier(inst, s.slack_webhooks[inst]))
    return out


def build_routes(s: config.Settings) -> dict[str, list[str]]:
    """Instance -> the notifier names its stale/failing alerts go to."""
    return {
        inst: [f"slack:{inst}" if ch == "slack" else ch for ch in s.channels_for(inst)]
        for inst in s.instances
    }


def build_checker(s: config.Settings, notifiers: list[Notifier]) -> Checker:
    """read_error is about the relay, not an instance: it goes to the global channels only."""
    return Checker(
        build_source(s),
        notifiers,
        stale_after=s.stale_after,
        read_fail_after=s.read_fail_after,
        instances=s.instances,
        routes=build_routes(s),
        read_error_to=list(s.global_channels()),
    )


def main(env: Mapping[str, str] = os.environ) -> int:
    try:
        settings = config.load(env)
    except config.ConfigError as e:
        print(f"resonant-relay: {e}", file=sys.stderr)
        return 2
    setup_logging(settings.secrets())

    site = web.load_site(settings.dist_dir, settings.supabase_url, settings.anon_key)
    notifiers = build_notifiers(settings)
    checker = build_checker(settings, notifiers)
    stop = threading.Event()
    thread = threading.Thread(
        target=checker.run, args=(settings.check_every, stop), name="checker", daemon=True
    )

    started = time.monotonic()
    # A check may make one read and several alert sends, each with its own timeout.
    limit = max(3 * settings.check_every + 30, 180)

    def healthy() -> bool:
        """The checker thread is alive and has finished a check recently."""
        last = checker.last_check if checker.last_check is not None else started
        return thread.is_alive() and time.monotonic() - last < limit

    def watchdog() -> None:
        # Fly only stops routing to a machine whose check fails; it restarts one whose
        # process exits. So a stuck or dead checker ends the process.
        while not stop.wait(settings.check_every):
            if not healthy():
                log.error("checker stopped checking; exiting so Fly restarts the machine")
                logging.shutdown()
                os._exit(1)

    server = web.make_server(
        settings.port, site, web.security_headers(settings.supabase_url), healthy
    )

    def on_signal(signum: int, _frame: FrameType | None) -> None:
        log.info("signal %d, stopping", signum)
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    thread.start()
    threading.Thread(target=watchdog, name="watchdog", daemon=True).start()
    log.info(
        "serving %d files on :%d; checking every %ds (stale after %ds, read mode %s, "
        "instances: %s, alerts: %s)",
        len(site),
        settings.port,
        settings.check_every,
        settings.stale_after,
        settings.read_mode,
        ", ".join(settings.instances),
        "; ".join(f"{i} -> {', '.join(r)}" for i, r in build_routes(settings).items()),
    )
    with server:
        server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
