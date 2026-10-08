// Regenerates the attributedBody fixtures in this directory.
//
//   cd tests/fixtures/imessage && swift make_blobs.swift
//
// Messages.app stores message.attributedBody as an NSArchiver "typedstream" of an
// NSMutableAttributedString whose runs carry __kIMMessagePartAttributeName. This builds the
// same shape with synthetic text, so no real message ever lands in the repo.
import Foundation

let fixtures: [(String, String)] = [
    ("plain.bin", "hello from the owner"),
    ("emoji.bin", "on my way 🚗💨 see you soon 👋🏽"),
    ("multiline.bin", "line one\nline two\n\nline four"),
    ("long.bin", String(repeating: "abcdefghij", count: 30)),  // 300 bytes: 2-byte length
    ("selftest.bin", "resonant-selftest:abcDEF12_-"),
]

for (name, text) in fixtures {
    let s = NSMutableAttributedString(string: text)
    s.addAttribute(
        NSAttributedString.Key("__kIMMessagePartAttributeName"),
        value: NSNumber(value: 0),
        range: NSRange(location: 0, length: (text as NSString).length))
    let data = NSArchiver.archivedData(withRootObject: s)
    try! data.write(to: URL(fileURLWithPath: name))
    print("\(name): \(data.count) bytes")
}
