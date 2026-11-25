import XCTest
@testable import FluentMon

@MainActor
final class MonitorClientTests: XCTestCase {
    func testBackoffSequence() {
        let delays = (0..<8).map { MonitorClient.delay(attempt: $0) }
        XCTAssertEqual(delays, [0.5, 1, 2, 4, 8, 10, 10, 10])
    }

    func testRejectedCodeIs4401() {
        XCTAssertEqual(MonitorClient.rejectedCode, 4401)
    }
}
