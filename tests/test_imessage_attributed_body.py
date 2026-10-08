"""attributedBody (typedstream) parsing. Fixture blobs: tests/fixtures/imessage/README.md."""

from __future__ import annotations

from pathlib import Path

import pytest

from resonant.gateway.imessage.attributed_body import parse_attributed_body

FIXTURES = Path(__file__).parent / "fixtures" / "imessage"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("plain.bin", "hello from the owner"),
        ("emoji.bin", "on my way 🚗💨 see you soon 👋🏽"),
        ("multiline.bin", "line one\nline two\n\nline four"),
        ("long.bin", "abcdefghij" * 30),
        ("selftest.bin", "resonant-selftest:abcDEF12_-"),
    ],
)
def test_fixtures(name: str, expected: str) -> None:
    assert parse_attributed_body((FIXTURES / name).read_bytes()) == expected


def test_int32_length() -> None:
    text = "y" * 70_000
    data = b"NSString\x01\x95\x84\x01+\x82" + len(text).to_bytes(4, "little") + text.encode()
    assert parse_attributed_body(data) == text


@pytest.mark.parametrize(
    "data",
    [
        None,
        b"",
        b"streamtyped but no string class",
        b"NSString without a string marker",
        b"NSString\x01\x95\x84\x01+",  # no length
        b"NSString\x01\x95\x84\x01+\x81\x05",  # truncated int16 length
        b"NSString\x01\x95\x84\x01+\x10short",  # length past the end
        b"NSString\x01\x95\x84\x01+\x02\xff\xfe",  # not utf-8
        b"NSString\x01\x95\x84\x01+\x90abc",  # unknown length tag
        b"NSString\x01\x95\x84\x01+\x81\xff\xff",  # negative length
    ],
)
def test_malformed_returns_none(data: bytes | None) -> None:
    assert parse_attributed_body(data) is None


def test_real_blobs_do_not_parse_after_truncation() -> None:
    data = (FIXTURES / "emoji.bin").read_bytes()
    marker = data.index(b"+") + 2
    assert parse_attributed_body(data[: marker + 5]) is None
