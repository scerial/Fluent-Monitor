import XCTest
@testable import FluentMon

final class SeriesBufferTests: XCTestCase {
    func testDecimateKeepsExtremesWithinBudget() {
        var points = (1...10_000).map { Point(iter: $0, value: 1.0) }
        points[5004] = Point(iter: 5005, value: 1e-9)
        points[7000] = Point(iter: 7001, value: 10)
        let out = SeriesBuffer.decimate(points, budget: 1200)
        XCTAssertLessThanOrEqual(out.count, 1200)
        XCTAssertTrue(out.contains { $0.iter == 5005 })
        XCTAssertTrue(out.contains { $0.iter == 7001 })
        XCTAssertEqual(out.map(\.iter), out.map(\.iter).sorted())
    }

    func testShortSeriesUnchanged() {
        let points = (1...10).map { Point(iter: $0, value: Double($0)) }
        XCTAssertEqual(SeriesBuffer.decimate(points, budget: 1200), points)
    }

    func testRangeCutoffs() {
        XCTAssertEqual(XRange.last100.cutoff(last: 1000), 901)
        XCTAssertEqual(XRange.last500.cutoff(last: 1000), 501)
    }
}
