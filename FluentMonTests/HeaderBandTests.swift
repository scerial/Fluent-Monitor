import XCTest
@testable import FluentMon

final class HeaderBandTests: XCTestCase {
    func testCaseTitle() {
        XCTAssertEqual(HeaderBand.caseTitle("FFF"), "Unnamed")
        XCTAssertEqual(HeaderBand.caseTitle("fff"), "Unnamed")
        XCTAssertEqual(HeaderBand.caseTitle(""), "Unnamed")
        XCTAssertEqual(HeaderBand.caseTitle(nil), "Unnamed")
        XCTAssertEqual(HeaderBand.caseTitle("duct_kw"), "duct_kw")
    }

    func testAgeFormats() {
        let now = Date(timeIntervalSince1970: 1_000_000)
        XCTAssertEqual(HeaderBand.age(since: now.addingTimeInterval(-0.4), now: now), "0.4 s ago")
        XCTAssertEqual(HeaderBand.age(since: now.addingTimeInterval(-12), now: now), "12 s ago")
        XCTAssertEqual(HeaderBand.age(since: now.addingTimeInterval(-180), now: now), "3 min ago")
        XCTAssertEqual(HeaderBand.age(since: now.addingTimeInterval(-3900), now: now), "1 h 05 min ago")
        XCTAssertEqual(HeaderBand.age(since: now.addingTimeInterval(5), now: now), "0 s")
        XCTAssertEqual(HeaderBand.age(since: nil, now: now), "—")
    }
}
