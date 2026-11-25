import Foundation

enum XRange: CaseIterable, Identifiable, Hashable {
    case all, last500, last100

    var id: Self { self }

    func cutoff(last: Int) -> Int {
        switch self {
        case .all: return Int.min
        case .last500: return last - 499
        case .last100: return last - 99
        }
    }
}

struct Point: Identifiable, Equatable {
    var id: Int { iter }
    let iter: Int
    let value: Double
}

struct Series: Identifiable {
    let id: Int
    let name: String
    var points: [Point]
}

enum SeriesBuffer {
    static let displayBudget = 1200

    static func decimate(_ points: [Point], budget: Int = displayBudget) -> [Point] {
        guard points.count > budget else { return points }
        let buckets = max(1, budget / 2)
        let size = (points.count + buckets - 1) / buckets
        var out: [Point] = []
        out.reserveCapacity(budget)
        var start = 0
        while start < points.count {
            let end = min(points.count, start + size)
            var lo = points[start], hi = points[start]
            for p in points[start..<end] {
                if p.value < lo.value { lo = p }
                if p.value > hi.value { hi = p }
            }
            for p in [lo, hi].sorted(by: { $0.iter < $1.iter }) where out.last?.iter != p.iter {
                out.append(p)
            }
            start = end
        }
        return out
    }
}
