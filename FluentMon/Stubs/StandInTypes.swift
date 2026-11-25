import Foundation
import Observation
import SwiftUI

enum DashboardGeometry {
    static let k: CGFloat = (1 + sin(.pi / 4)) / 2
}

enum Palette {
    static let cool = Color(red: 0x3B / 255, green: 0xA7 / 255, blue: 1)
    static let hot = Color.orange
    static let fluent = Color(red: 1, green: 0x2D / 255, blue: 0x95 / 255)
    static let residuals: [Color] = [.red, .blue, .green, .orange, .purple, .cyan, .pink, .mint]
}

struct Segment: Identifiable {
    let id: Int
    let from: Double
    let to: Double
    let color: Color
}

struct Meter: Identifiable {
    let id: Int
    let title: String
    let major: String
    let minor: String?
    var minorColor: Color = .primary.opacity(0.85)
    let outer: [Segment]
    var inner: [Segment] = []
}

struct Session: Equatable {
    enum Mode: Equatable { case steady, transient }
    enum Status: Equatable { case running, converged, idle, problem }

    var caseName: String?
    var mode: Mode
    var status: Status
}

enum Connection: Equatable {
    case live, stale, offline
}

enum XRange: CaseIterable, Identifiable, Hashable {
    case all, last500, last100

    var id: Self { self }
}

struct Point: Identifiable, Equatable {
    var id: Int { iter }
    let iter: Int
    let value: Double
}

struct Series: Identifiable {
    let id: Int
    let name: String
    let points: [Point]
}

@Observable
final class MonitorStore {
    var series: [Series]
    var target: Double
    var scrubIter: Int?

    init(series: [Series] = [], target: Double = 1e-6) {
        self.series = series
        self.target = target
    }

    var names: [String] { series.map(\.name) }

    func visibleSeries(range: XRange) -> [Series] {
        let cut = cutoff(range)
        return series.map { s in
            Series(id: s.id, name: s.name, points: s.points.filter { $0.iter >= cut })
        }
    }

    func xDomain(range: XRange) -> ClosedRange<Int> {
        let iters = series.flatMap(\.points).map(\.iter)
        guard let last = iters.max(), let first = iters.min() else { return 0...1 }
        let lower = max(first, cutoff(range))
        return lower...max(lower + 1, last)
    }

    var yDomain: ClosedRange<Double> {
        let values = series.flatMap(\.points).map(\.value).filter { $0 > 0 }
        let low = min(values.min() ?? target, target / 10)
        let high = max(values.max() ?? 1, target)
        let a = pow(10, floor(log10(low)))
        let b = pow(10, ceil(log10(high)))
        return a...max(b, a * 10)
    }

    private func cutoff(_ range: XRange) -> Int {
        guard let last = series.flatMap(\.points).map(\.iter).max() else { return 0 }
        switch range {
        case .all: return Int.min
        case .last500: return last - 499
        case .last100: return last - 99
        }
    }
}

struct ConsoleLine: Identifiable, Equatable {
    let id: Int
    let text: String
    let kind: Character
}

struct ConsoleBuffer {
    private(set) var lines: [ConsoleLine]

    init(lines: [ConsoleLine] = []) {
        self.lines = lines
    }

    mutating func append(first: Int, lines new: [String], kinds: String) {
        let lastID = lines.last?.id ?? Int.min
        let chars = Array(kinds)
        for (offset, text) in new.enumerated() where first + offset > lastID {
            let kind = offset < chars.count ? chars[offset] : "n"
            lines.append(ConsoleLine(id: first + offset, text: text, kind: kind))
        }
        if lines.count > 5000 {
            lines.removeFirst(lines.count - 5000)
        }
    }
}

struct DiscoveredServer: Identifiable, Hashable {
    var id: String { "\(host):\(port)" }
    let name: String
    let host: String
    let port: Int
}

struct Endpoint: Equatable {
    var url: URL
    var token: String
}

struct ClientSettings: Equatable {
    var target: Double = 1e-6
    var exclude: [String] = []
    var meterCpus: [Int] = [0, 4, 8, 12]
}

enum ConvState: Equatable {
    case ok, stalled, rising, nan
}

struct BarRow: Equatable {
    let p: Double
    let label: String
}

struct ConvRow: Equatable {
    let p: Double
    let of: Double
    let state: ConvState
    let label: String
}

struct Bars: Equatable {
    let iter: BarRow
    let converged: Bool
    let conv: ConvRow
    let step: BarRow?
}

struct BarMetrics: Equatable {
    var scale: CGFloat = 1
    var showCaptions = true
    var padV: CGFloat { 14 * scale }
    var row: CGFloat { (22 + 8 + 12 + (showCaptions ? 6 + 18 : 0)) * scale }
    var rowGap: CGFloat { 14 * scale }
    var height: CGFloat { 2 * padV + 3 * row + 2 * rowGap }
    var compact: BarMetrics { var c = self; c.showCaptions = false; return c }
}
