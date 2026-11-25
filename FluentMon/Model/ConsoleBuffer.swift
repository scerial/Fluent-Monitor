import Foundation

struct ConsoleLine: Identifiable, Equatable {
    let id: Int
    let text: String
    let kind: Character
}

struct ConsoleBuffer {
    static let capacity = 5000

    private(set) var lines: [ConsoleLine]

    init(lines: [ConsoleLine] = []) {
        self.lines = Array(lines.suffix(Self.capacity))
    }

    mutating func reset(first: Int, lines new: [String], kinds: String) {
        lines = []
        append(first: first, lines: new, kinds: kinds)
    }

    mutating func append(first: Int, lines new: [String], kinds: String) {
        let lastID = lines.last?.id ?? Int.min
        let chars = Array(kinds)
        for (offset, text) in new.enumerated() where first + offset > lastID {
            lines.append(ConsoleLine(id: first + offset, text: text, kind: Self.kind(chars, offset)))
        }
        if lines.count > Self.capacity {
            lines.removeFirst(lines.count - Self.capacity)
        }
    }

    mutating func prepend(first: Int, lines new: [String], kinds: String) {
        let firstID = lines.first?.id ?? Int.max
        let chars = Array(kinds)
        var older: [ConsoleLine] = []
        for (offset, text) in new.enumerated() where first + offset < firstID {
            older.append(ConsoleLine(id: first + offset, text: text, kind: Self.kind(chars, offset)))
        }
        lines = older + lines
        if lines.count > Self.capacity {
            lines.removeLast(lines.count - Self.capacity)
        }
    }

    private static func kind(_ chars: [Character], _ offset: Int) -> Character {
        offset < chars.count ? chars[offset] : "n"
    }
}
