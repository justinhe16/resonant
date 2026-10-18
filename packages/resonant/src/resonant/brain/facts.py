"""Long-term facts (docs/brain-plan.md D9): one small Markdown file per fact.

A fact lives at ``<home>/brain/facts/<name>.md``::

    ---
    name: mycelium-path
    description: mycelium is checked out at ~/code/mycelium, not ~/code/myc
    ---
    Optional longer text the index doesn't show (list_facts does).

The file name is the truth: a ``name:`` line that disagrees is ignored, so a hand-edited or
renamed file can't claim another fact's slot. The index (``render_index``: one line per fact,
``- <name>: <description>``, by name) goes into the brain's stable prompt prefix, so it changes
only when a fact does.

Facts are the owner's own text (only the owner can ``remember``), not untrusted data, but
they still go through ``clean``: control and format characters (escape sequences, bidi
overrides, zero-width) are dropped, so a pasted fact can't hide anything in the prompt.
Caps: ``FACTS_MAX`` facts, ``DESCRIPTION_MAX`` and ``TEXT_MAX`` characters, ``FILE_MAX``
bytes per file (``remember`` refuses a bigger one; a bigger file on disk is skipped),
``INDEX_MAX_CHARS`` for the rendered index.

Writes are atomic and durable (``mkstemp`` in the same dir, fsync, ``os.replace``); the
dir is 0700 and each file 0600. Reads open with ``O_NOFOLLOW`` and take regular files only.

``remember`` is L3 (D9, the owner's call) and the index sits in every later prompt, so a
task that read injected text could plant a fact. The brain loop depends on D6: a tainted
task's calls ask first even at L3. The report line shows the whole description. Nothing
here touches the database.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from resonant.brain.text import ANGLES, drop_controls

FACTS_MAX = 50
NAME_MAX = 40
DESCRIPTION_MAX = 120
TEXT_MAX = 2000
FILE_MAX = 16384  # bytes read per file; a bigger file is not a fact (skipped)
INDEX_MAX_CHARS = 4000  # the index in the prompt; the rest shows as "+N more"
SUFFIX = ".md"

NAME = re.compile(rf"[a-z0-9][a-z0-9-]{{0,{NAME_MAX - 1}}}")
_FENCE = "---"


class FactError(ValueError):
    """A bad name, description or text, a full store, or an unknown fact."""


@dataclass(frozen=True)
class Fact:
    name: str
    description: str
    text: str  # the body; may be empty


def facts_dir(home: Path) -> Path:
    """``<home>/brain/facts``: where the facts of the Resonant at ``home`` live."""
    return home / "brain" / "facts"


def clean(text: str, *, single_line: bool) -> str:
    """``text`` without control or format characters (Unicode categories Cc and Cf). Tabs
    become spaces; newlines are kept unless ``single_line``, where every run of whitespace
    becomes one space. Trailing spaces on each line and blank lines at the ends go."""
    norm = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\t", " ")
    keep = drop_controls(norm)
    if single_line:
        return re.sub(r"\s+", " ", keep).strip()
    lines = [line.rstrip() for line in keep.split("\n")]
    return "\n".join(lines).strip("\n").strip()


def slugify(text: str) -> str:
    """A fact name from free text (``Mycelium path!`` -> ``mycelium-path``), or '' if none."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:NAME_MAX].rstrip("-")


def _parse(name: str, raw: str) -> Fact | None:
    """A fact file's text, or None when it has no frontmatter or no description."""
    lines = raw.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != _FENCE:
        return None
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == _FENCE)
    except StopIteration:
        return None
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip().lower()] = value.strip()
    description = clean(meta.get("description", ""), single_line=True)[:DESCRIPTION_MAX]
    if not description:
        return None
    body = clean("\n".join(lines[end + 1 :]), single_line=False)[:TEXT_MAX]
    return Fact(name=name, description=description, text=body)


def _render(fact: Fact) -> str:
    body = f"{fact.text}\n" if fact.text else ""
    return f"{_FENCE}\nname: {fact.name}\ndescription: {fact.description}\n{_FENCE}\n{body}"


class FactStore:
    """The facts under ``root`` (``facts_dir(settings.home)``). Reads tolerate a missing dir."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, name: str) -> Path:
        return self.root / f"{name}{SUFFIX}"

    def _read(self, path: Path) -> Fact | None:
        name = path.name.removesuffix(SUFFIX)
        if NAME.fullmatch(name) is None:
            return None
        try:  # O_NOFOLLOW: a symlink fails to open; fstat: a regular file only
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            return None
        try:
            with os.fdopen(fd, "rb") as f:
                if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                    return None
                data = f.read(FILE_MAX + 1)
        except OSError:
            return None
        if len(data) > FILE_MAX:
            return None
        return _parse(name, data.decode("utf-8", errors="replace"))

    def list(self) -> list[Fact]:
        """Every readable fact, by name. Malformed, oversized or oddly named files are
        skipped, never an error."""
        try:
            paths = sorted(self.root.glob(f"*{SUFFIX}"))
        except OSError:
            return []
        facts = [f for f in (self._read(p) for p in paths) if f is not None]
        return facts[:FACTS_MAX]

    def get(self, name: str) -> Fact | None:
        if NAME.fullmatch(name) is None:
            return None
        return self._read(self._path(name))

    def remember(self, name: str, description: str, text: str = "") -> tuple[Fact, bool]:
        """Save (or replace) the fact ``name``. Returns it and whether it is new."""
        if NAME.fullmatch(name) is None:
            raise FactError(
                f"name must be a slug: lowercase letters, digits and dashes, at most {NAME_MAX}"
            )
        description = clean(description, single_line=True)
        if not description:
            raise FactError("description must not be empty")
        if len(description) > DESCRIPTION_MAX:
            raise FactError(
                f"description is {len(description)} characters (at most {DESCRIPTION_MAX})"
            )
        text = clean(text, single_line=False)
        if len(text) > TEXT_MAX:
            raise FactError(f"text is {len(text)} characters (at most {TEXT_MAX})")
        path = self._path(name)
        new = not path.exists()
        if new and len(self.list()) >= FACTS_MAX:
            raise FactError(f"already {FACTS_MAX} facts; forget one first")
        fact = Fact(name=name, description=description, text=text)
        data = _render(fact).encode("utf-8")
        if len(data) > FILE_MAX:  # it couldn't be read back
            raise FactError(f"fact is {len(data)} bytes (at most {FILE_MAX})")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        fd, tmp_name = tempfile.mkstemp(dir=self.root, prefix=f".{name}.", suffix=".tmp")
        tmp = Path(tmp_name)  # mkstemp: 0600, O_EXCL, a unique name
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return fact, new

    def forget(self, name: str) -> Fact:
        """Delete the fact ``name`` and return what it was. A file under a valid name that
        doesn't read as a fact (malformed, oversized, a symlink) is deleted too."""
        if NAME.fullmatch(name) is None:
            raise FactError(f"no fact {name!r}")
        path = self._path(name)
        fact = self._read(path) or Fact(name=name, description="(unreadable)", text="")
        try:
            path.unlink()
        except FileNotFoundError:
            raise FactError(f"no fact {name!r}") from None
        return fact


def render_index(facts: list[Fact], *, max_chars: int = INDEX_MAX_CHARS) -> str:
    """``- <name>: <description>`` per fact, by name, inside ``<facts>…</facts>``; '' with
    none. Angle brackets become spaces so a description can't close the tag. Past
    ``max_chars`` the rest is ``- +N more (list_facts)``."""
    if not facts:
        return ""
    lines: list[str] = []
    used = 0
    ordered = sorted(facts, key=lambda f: f.name)
    for i, fact in enumerate(ordered):
        line = f"- {fact.name}: {fact.description.translate(ANGLES)}"
        if used + len(line) + 1 > max_chars:
            lines.append(f"- +{len(ordered) - i} more (list_facts)")
            break
        lines.append(line)
        used += len(line) + 1
    return "<facts>\n" + "\n".join(lines) + "\n</facts>"
