import XCTest
@testable import FluentMon

final class ConsoleBufferTests: XCTestCase {
    func testAppendIgnoresDuplicates() {
        var buffer = ConsoleBuffer()
        buffer.append(first: 1, lines: ["a", "b"], kinds: "nn")
        buffer.append(first: 2, lines: ["b", "c"], kinds: "nn")
        XCTAssertEqual(buffer.lines.map(\.id), [1, 2, 3])
        XCTAssertEqual(buffer.lines.map(\.text), ["a", "b", "c"])
    }

    func testCapDropsOldestLines() {
        var buffer = ConsoleBuffer()
        buffer.append(first: 1, lines: (1...5010).map { "line \($0)" }, kinds: "")
        XCTAssertEqual(buffer.lines.count, ConsoleBuffer.capacity)
        XCTAssertEqual(buffer.lines.first?.id, 11)
        XCTAssertEqual(buffer.lines.last?.id, 5010)
    }

    func testPrependKeepsOrder() {
        var buffer = ConsoleBuffer()
        buffer.reset(first: 100, lines: ["x"], kinds: "n")
        buffer.prepend(first: 98, lines: ["y", "z", "x"], kinds: "wh")
        XCTAssertEqual(buffer.lines.map(\.id), [98, 99, 100])
        XCTAssertEqual(buffer.lines.map(\.text), ["y", "z", "x"])
        XCTAssertEqual(buffer.lines.map(\.kind), ["w", "h", "n"])
    }

    func testShortKindsDefaultToNormal() {
        var buffer = ConsoleBuffer()
        buffer.append(first: 1, lines: ["a", "b"], kinds: "e")
        XCTAssertEqual(buffer.lines.map(\.kind), ["e", "n"])
    }
}
