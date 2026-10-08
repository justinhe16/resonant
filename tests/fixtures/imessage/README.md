# iMessage `attributedBody` fixtures

Each `*.bin` file is a `message.attributedBody` blob: an `NSArchiver` typedstream of an
`NSMutableAttributedString` with a `__kIMMessagePartAttributeName` run, the same shape
Messages.app writes to `chat.db`. The text is synthetic. No real message is in this repo.

| File | Text |
|---|---|
| `plain.bin` | `hello from the owner` |
| `emoji.bin` | `on my way 🚗💨 see you soon 👋🏽` (multi-byte UTF-8, skin-tone modifier) |
| `multiline.bin` | `line one\nline two\n\nline four` |
| `long.bin` | `abcdefghij` × 30 (300 bytes, so the length uses the 2-byte `0x81` form) |
| `selftest.bin` | `resonant-selftest:abcDEF12_-` (a self-test nonce message) |

They were made on macOS with Foundation's own archiver, so they are byte-for-byte what the
OS produces:

```sh
cd tests/fixtures/imessage && swift make_blobs.swift
```

To add a case, append to the `fixtures` list in `make_blobs.swift`, rerun it, and add the
expected text to `tests/test_imessage_attributed_body.py`.
