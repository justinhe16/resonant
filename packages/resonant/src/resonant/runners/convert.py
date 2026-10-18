"""Media conversions for the brain (BR-7, docs/brain-plan.md D10): HEIC to JPEG for Claude,
audio to a 16 kHz WAV for whisper, and the whisper call itself. Blocking: callers run them in
a thread (``asyncio.to_thread``).

- **HEIC.** Claude takes JPEG, PNG, GIF and WebP images, not HEIC (the vision docs list the
  four), so an iPhone photo is converted with macOS ``sips`` (``-Z 2048`` keeps the long side
  at most 2048 px). ``sips`` missing or failing leaves the HEIC as it is.
- **Audio.** whisper.cpp reads 16 kHz WAV; ``afconvert`` (macOS) converts Messages' ``.caf``
  and ``.m4a``. Missing or failing, whisper gets the original file.
- **Whisper.** ``brain.whisper_path`` with ``-m brain.whisper_model`` (when set) and
  ``brain.whisper_args`` (``{input}`` replaced); the transcript is stdout, cleaned: control
  characters dropped, whitespace collapsed, whisper's ``[BLANK_AUDIO]``-style tags removed.

Argv only, never a shell; the environment is a minimal one (``PATH`` of system dirs, ``HOME``).
Outputs are never logged (a transcript is a message body).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from resonant.config import BrainConfig

log = logging.getLogger(__name__)

SIPS = "/usr/bin/sips"
AFCONVERT = "/usr/bin/afconvert"
CONVERT_TIMEOUT_S = 60.0
HEIC_SUFFIXES = frozenset({".heic", ".heif"})
IMAGE_MAX_PX = 2048
TRANSCRIPT_MAX = 4000
_TAG = re.compile(r"\[[A-Z_ ]+\]|\([A-Za-z ]+\)")  # [BLANK_AUDIO], (music), (silence)
_ENV = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"}


class ConvertError(Exception):
    """A conversion or transcription failed; ``str(e)`` is short and body-free."""


def _env() -> dict[str, str]:
    return {**_ENV, "HOME": os.environ.get("HOME", "/")}


def _run(argv: Sequence[str], timeout_s: float, cwd: Path | None = None) -> str:
    try:
        done = subprocess.run(  # noqa: S603 - argv, no shell
            list(argv),
            cwd=cwd,
            env=_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise ConvertError(f"{Path(argv[0]).name} took over {timeout_s:g}s") from e
    except OSError as e:
        raise ConvertError(f"{Path(argv[0]).name} couldn't start: {e.strerror}") from e
    if done.returncode != 0:
        raise ConvertError(f"{Path(argv[0]).name} exited {done.returncode}")
    return done.stdout.decode(errors="replace")


def heic_to_jpeg(src: Path, sips: str = SIPS) -> Path:
    """Convert ``src`` (a HEIC/HEIF in the ask's own folder) to a JPEG next to it and remove
    the HEIC. Returns the file to give Claude: the JPEG, or ``src`` when it can't convert."""
    if src.suffix.lower() not in HEIC_SUFFIXES or not Path(sips).exists():
        return src
    dest = src.with_suffix(".jpg")
    try:
        _run(
            [sips, "-s", "format", "jpeg", "-Z", str(IMAGE_MAX_PX), str(src), "--out", str(dest)],
            CONVERT_TIMEOUT_S,
        )
    except ConvertError as e:
        log.warning("heic conversion failed (%s); sending it as is", e)
        return src
    if not dest.is_file() or dest.stat().st_size == 0:
        return src
    src.unlink(missing_ok=True)
    return dest


def to_wav(src: Path, dest: Path, afconvert: str = AFCONVERT) -> Path:
    """``src`` as a 16 kHz mono WAV at ``dest``, or ``src`` when afconvert is missing or
    fails (whisper may still read it)."""
    if not Path(afconvert).exists():
        return src
    try:
        _run(
            [afconvert, "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(src), str(dest)],
            CONVERT_TIMEOUT_S,
        )
    except ConvertError as e:
        log.warning("audio conversion failed (%s); giving whisper the original", e)
        return src
    return dest if dest.is_file() and dest.stat().st_size > 0 else src


def whisper_argv(cfg: BrainConfig, audio: Path) -> list[str]:
    """The whisper call for ``audio``. ``cfg.whisper_path`` must be set."""
    assert cfg.whisper_path is not None
    argv = [str(cfg.whisper_path)]
    if cfg.whisper_model is not None:
        argv += ["-m", str(cfg.whisper_model)]
    return argv + [a.replace("{input}", str(audio)) for a in cfg.whisper_args]


def clean_transcript(raw: str, limit: int = TRANSCRIPT_MAX) -> str:
    """One paragraph of plain text: tags and control characters dropped, clipped."""
    text = _TAG.sub(" ", raw)
    text = "".join(c if c.isprintable() else " " for c in text)
    return " ".join(text.split())[:limit].strip()


def transcribe(cfg: BrainConfig, src: Path, work: Path, *, afconvert: str = AFCONVERT) -> str:
    """Transcribe ``src`` (already copied into ``work``, the task's own folder) with whisper.
    Returns the cleaned transcript ('' when whisper heard nothing); ``ConvertError`` when it
    failed."""
    if cfg.whisper_path is None:
        raise ConvertError("whisper isn't set up")
    audio = to_wav(src, work / "voice.wav", afconvert)
    out = _run(whisper_argv(cfg, audio), cfg.whisper_timeout_s, cwd=work)
    return clean_transcript(out)


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


__all__ = [
    "ConvertError",
    "clean_transcript",
    "heic_to_jpeg",
    "remove_tree",
    "to_wav",
    "transcribe",
    "whisper_argv",
]
