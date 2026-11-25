import XCTest
import UIKit
@testable import FluentMon

final class DashboardGeometryTests: XCTestCase {
    private let scale: CGFloat = 2
    private let tolerance: CGFloat = 0.51

    private let sizes: [CGSize] = [
        CGSize(width: 1194, height: 834),
        CGSize(width: 1366, height: 1024),
        CGSize(width: 834, height: 1194),
        CGSize(width: 1024, height: 1366)
    ]

    func testLandscapeAlignmentRules() {
        for size in sizes where size.width > size.height {
            let g = DashboardGeometry(size: size, mode: .landscape, barsHeight: 254, displayScale: scale)
            let gap = DashboardGeometry.gap, pad = DashboardGeometry.pad
            XCTAssertEqual(g.header.minY, pad, accuracy: 0.01, "\(size)")
            XCTAssertEqual(g.header.maxY + gap, g.chart.minY, accuracy: tolerance, "\(size)")
            XCTAssertEqual(g.chart.minY, g.meters[0].minY, accuracy: tolerance, "\(size)")
            XCTAssertEqual(g.meters.last!.maxY, size.height - pad, accuracy: 0.01, "\(size)")
            XCTAssertEqual(g.bars.maxY, size.height - pad, accuracy: 0.01, "\(size)")
            XCTAssertEqual(g.chart.maxY + gap, g.bars.minY, accuracy: tolerance, "\(size)")
            for i in 0..<5 {
                XCTAssertEqual(g.meters[i + 1].minY - g.meters[i].maxY,
                               DashboardGeometry.vGapRatio * g.gaugeDiameter, accuracy: tolerance, "\(size)")
            }
            for m in g.meters {
                XCTAssertEqual(m.maxX, size.width - pad, accuracy: tolerance, "\(size)")
            }
            XCTAssertEqual(g.chart.width + gap + g.meters[0].width, size.width - 2 * pad, accuracy: tolerance, "\(size)")
            assertSnapped(g, size)
        }
    }

    func testPortraitAlignmentRules() {
        for size in sizes where size.height > size.width {
            let g = DashboardGeometry(size: size, mode: .portrait, barsHeight: 254, displayScale: scale)
            let gap = DashboardGeometry.gap, pad = DashboardGeometry.pad
            XCTAssertEqual(g.header.maxY + gap, g.meters[0].minY, accuracy: tolerance, "\(size)")
            for m in g.meters {
                XCTAssertEqual(m.minY, g.meters[0].minY, accuracy: 0.01, "\(size)")
            }
            for i in 0..<5 {
                XCTAssertEqual(g.meters[i + 1].minX - g.meters[i].maxX,
                               DashboardGeometry.hGapRatio * g.gaugeDiameter, accuracy: tolerance, "\(size)")
            }
            XCTAssertEqual(g.meters.last!.maxX, size.width - pad, accuracy: tolerance, "\(size)")
            XCTAssertEqual(g.chart.minY, g.meters[0].maxY + gap, accuracy: tolerance, "\(size)")
            XCTAssertEqual(g.bars.maxY, size.height - pad, accuracy: 0.01, "\(size)")
            XCTAssertGreaterThanOrEqual(g.chart.height, 220, "\(size)")
            assertSnapped(g, size)
        }
    }

    private func assertSnapped(_ g: DashboardGeometry, _ size: CGSize, file: StaticString = #filePath, line: UInt = #line) {
        let frames = [g.header, g.chart, g.bars] + g.meters
        for f in frames {
            for edge in [f.minX, f.minY, f.maxX, f.maxY] {
                let steps = edge * scale
                XCTAssertEqual(steps, steps.rounded(), accuracy: 0.0001, "\(size)", file: file, line: line)
            }
        }
    }

    func testLayoutModeFullScreenFollowsOrientation() {
        let size = CGSize(width: 834, height: 1194)
        XCTAssertEqual(LayoutMode.resolve(size: size, isFullScreen: true, orientation: .portraitUpsideDown, previous: .landscape), .portrait)
        XCTAssertEqual(LayoutMode.resolve(size: size, isFullScreen: true, orientation: .portrait, previous: .landscape), .portrait)
        XCTAssertEqual(LayoutMode.resolve(size: size, isFullScreen: true, orientation: .landscapeLeft, previous: .portrait), .landscape)
        XCTAssertEqual(LayoutMode.resolve(size: size, isFullScreen: true, orientation: .landscapeRight, previous: .portrait), .landscape)
    }

    func testLayoutModeWindowedHasHysteresis() {
        func resolve(_ width: CGFloat, _ previous: DashboardGeometry.Mode) -> DashboardGeometry.Mode {
            LayoutMode.resolve(size: CGSize(width: width, height: 1000), isFullScreen: false,
                               orientation: .unknown, previous: previous)
        }
        XCTAssertEqual(resolve(900, .landscape), .portrait)
        XCTAssertEqual(resolve(1000, .landscape), .landscape)
        XCTAssertEqual(resolve(1000, .portrait), .portrait)
        XCTAssertEqual(resolve(1100, .portrait), .landscape)
    }
}
