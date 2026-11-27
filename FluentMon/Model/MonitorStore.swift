import Foundation
import Observation
import SwiftUI

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
    enum Mode: Equatable {
        case steady, transient

        init(wire: String) {
            self = wire == "steady" ? .steady : .transient
        }
    }

    enum Status: Equatable {
        case running, converged, idle, problem

        init(wire: String) {
            switch wire {
            case "running": self = .running
            case "converged": self = .converged
            case "problem": self = .problem
            default: self = .idle
            }
        }
    }

    var caseName: String?
    var mode: Mode
    var status: Status
}

enum Connection: Equatable {
    case live, stale, offline
}

enum ConvState: Equatable {
    case ok, stalled, rising, nan

    init(wire: String) {
        switch wire {
        case "ok": self = .ok
        case "stalled": self = .stalled
        case "rising": self = .rising
        default: self = .nan
        }
    }
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

struct ClientSettings: Equatable {
    var target: Double = 1e-6
    var exclude: [String] = []
    var meterCpus: [Int] = [0, 4, 8, 12]
}

extension Thermal {
    func fraction(_ t: Double) -> Double {
        min(1, max(0, (t - baseC) / (maxC - baseC)))
    }

    func color(_ t: Double) -> Color {
        if fraction(t) > 0.9 { return .red }
        if t > warnC { return .yellow }
        return Palette.cool
    }
}

private func fixed(_ value: Double, _ digits: Int) -> String {
    String(format: "%.\(digits)f", locale: Locale(identifier: "en_US_POSIX"), value)
}

private func clock(_ seconds: Double) -> String {
    let total = Int(seconds.rounded())
    return String(format: "%d:%02d:%02d", total / 3600, (total % 3600) / 60, total % 60)
}

@Observable
@MainActor
final class MonitorStore {
    private(set) var session: Session?
    private(set) var series: [Series] = []
    private(set) var bars: Bars?
    private(set) var console = ConsoleBuffer()
    private(set) var thermal = Thermal(baseC: 20, warnC: 70, maxC: 89)
    private(set) var connection: Connection = .offline
    private(set) var sysAt: Date?
    private(set) var lastIterationAt: Date?
    private(set) var sys: SysMsg?
    private(set) var cpuSmoothed: [Int: Double] = [:]
    var scrubIter: Int?
    var settings = ClientSettings()

    private var isOffline = true
    private var resColumns: [(name: String, index: Int)] = []

    init() {}

    var target: Double { settings.target }

    var names: [String] {
        series.map(\.name).filter { !settings.exclude.contains($0) }
    }

    private var lastIter: Int? {
        series.compactMap { $0.points.last?.iter }.max()
    }

    private var firstIter: Int? {
        series.compactMap { $0.points.first?.iter }.min()
    }

    func visibleSeries(range: XRange) -> [Series] {
        let cut = range.cutoff(last: lastIter ?? 0)
        return series.filter { !settings.exclude.contains($0.name) }.map { s in
            let clipped = s.points.filter { $0.iter >= cut && $0.value > 0 && $0.value.isFinite }
            return Series(id: s.id, name: s.name, points: SeriesBuffer.decimate(clipped))
        }
    }

    func xDomain(range: XRange) -> ClosedRange<Int> {
        guard let last = lastIter, let first = firstIter else { return 0...1 }
        let lower = max(first, range.cutoff(last: last))
        return lower...max(lower + 1, last)
    }

    var yDomain: ClosedRange<Double> {
        let values = series.flatMap(\.points).map(\.value).filter { $0 > 0 && $0.isFinite }
        let low = min(values.min() ?? target, target / 10)
        let high = max(values.max() ?? 1, target)
        let a = pow(10, floor(log10(low)))
        let b = pow(10, ceil(log10(high)))
        return a...max(b, a * 10)
    }

    var meters: [Meter] {
        var result = settings.meterCpus.enumerated().map { index, id in cpuMeter(index: index, id: id) }
        result.append(powerMeter(id: result.count))
        result.append(ramMeter(id: result.count))
        return result
    }

    func apply(_ message: Incoming, now: Date) {
        switch message {
        case .hello(let hello): thermal = hello.thermal
        case .snapshot(let snapshot): applySnapshot(snapshot, now: now)
        case .resid(let resid): applyResid(resid, now: now)
        case .bars(let msg): bars = makeBars(msg)
        case .sys(let msg): applySys(msg, now: now)
        case .con(let msg): applyConsole(msg)
        case .event(let event): applyEvent(event)
        }
    }

    func setOffline(_ offline: Bool) {
        isOffline = offline
        refreshConnection(now: .now)
    }

    func refreshConnection(now: Date) {
        if isOffline {
            connection = .offline
        } else if let sysAt, now.timeIntervalSince(sysAt) <= 7 {
            connection = .live
        } else {
            connection = .stale
        }
    }

    private func applySnapshot(_ snapshot: SnapshotMsg, now: Date) {
        session = Session(caseName: snapshot.session.caseName,
                          mode: Session.Mode(wire: snapshot.session.mode),
                          status: Session.Status(wire: snapshot.session.status))
        resColumns = snapshot.columns.enumerated().compactMap { index, name in
            index < snapshot.kinds.count && snapshot.kinds[index] == "res" ? (name, index) : nil
        }
        series = resColumns.enumerated().map { id, column in
            let row = column.index < snapshot.values.count ? snapshot.values[column.index] : []
            let points = snapshot.iters.enumerated().compactMap { j, iter -> Point? in
                guard j < row.count, let value = row[j] else { return nil }
                return Point(iter: iter, value: value)
            }
            return Series(id: id, name: column.name, points: points)
        }
        bars = snapshot.bars.map(makeBars)
        console = ConsoleBuffer()
        if let tail = snapshot.conTail {
            console.reset(first: tail.first, lines: tail.lines, kinds: tail.kinds)
        }
        if !snapshot.iters.isEmpty {
            lastIterationAt = now
        }
    }

    private func applyResid(_ resid: ResidMsg, now: Date) {
        var added = false
        for (k, column) in resColumns.enumerated() where k < series.count {
            for row in resid.rows {
                guard row.values.indices.contains(column.index), let value = row.values[column.index] else { continue }
                if (series[k].points.last?.iter ?? Int.min) >= row.iter { continue }
                series[k].points.append(Point(iter: row.iter, value: value))
                added = true
            }
        }
        if added {
            lastIterationAt = now
        }
    }

    private func applySys(_ msg: SysMsg, now: Date) {
        sys = msg
        sysAt = now
        for (i, id) in msg.cpuIds.enumerated() where i < msg.cpu.count {
            guard let value = msg.cpu[i] else { continue }
            cpuSmoothed[id] = cpuSmoothed[id].map { 0.5 * $0 + 0.5 * value } ?? value
        }
    }

    private func applyConsole(_ msg: ConMsg) {
        if let first = console.lines.first?.id, msg.first < first {
            console.prepend(first: msg.first, lines: msg.lines, kinds: msg.kinds)
        } else {
            console.append(first: msg.first, lines: msg.lines, kinds: msg.kinds)
        }
    }

    private func applyEvent(_ event: EventMsg) {
        switch event.kind {
        case "run_start", "resumed": session?.status = .running
        case "converged": session?.status = .converged
        case "problem": session?.status = .problem
        case "idle", "session_end": session?.status = .idle
        default: break
        }
    }

    private func makeBars(_ msg: BarsMsg) -> Bars {
        var iterRow = BarRow(p: 0, label: "")
        if let iter = msg.iter {
            var iterLabel = "\(iter.left) left"
            if let eta = iter.etaS { iterLabel += " · ETA \(clock(eta))" }
            iterRow = BarRow(p: iter.p, label: iterLabel)
        }
        var convRow = ConvRow(p: 0, of: 0, state: .ok, label: "")
        if let conv = msg.conv {
            var convLabel = "Limiting: \(conv.limiting ?? "—") "
            convLabel += conv.rmax.map { String(format: "%.1e", locale: Locale(identifier: "en_US_POSIX"), $0) } ?? "—"
            convLabel += " · \(fixed(conv.decades ?? 0, 1)) / \(fixed(conv.of, 1)) decades"
            if let iters = conv.itersToTarget { convLabel += " · ≈ \(iters.formatted()) iters" }
            convRow = ConvRow(p: conv.p, of: conv.of, state: ConvState(wire: conv.state), label: convLabel)
        }
        var stepRow: BarRow?
        if let step = msg.step {
            var stepLabel = "t = \(fixed(step.flowTime, 4))"
            if let tEnd = step.tEnd { stepLabel += " / \(fixed(tEnd, 4))" }
            stepLabel += " s"
            if let eta = step.etaS { stepLabel += " · ETA \(clock(eta))" }
            stepRow = BarRow(p: step.p, label: stepLabel)
        }
        return Bars(iter: iterRow,
                    converged: session?.status == .converged,
                    conv: convRow,
                    step: stepRow)
    }

    private func cpuMeter(index: Int, id: Int) -> Meter {
        guard let sys, let i = sys.cpuIds.firstIndex(of: id) else {
            return Meter(id: index, title: "CPU \(id)", major: "—", minor: "—", outer: [])
        }
        let load = cpuSmoothed[id]
        let clock = i < sys.clkMhz.count ? sys.clkMhz[i] : nil
        return Meter(id: index, title: "CPU \(id)",
                     major: load.map { "\(Int($0.rounded())) %" } ?? "—",
                     minor: clock.map { "\(fixed($0 / 1000, 1)) GHz" } ?? "—",
                     outer: load.map { [Segment(id: 0, from: 0, to: $0 / 100, color: Palette.cool)] } ?? [])
    }

    private func powerMeter(id: Int) -> Meter {
        let watts = sys?.pkgW
        let temp = sys?.tctlC
        let color = temp.map { thermal.color($0) } ?? Palette.cool
        let maxW = sys?.pkgMaxW ?? 162
        let tdp = sys?.tdpW ?? 120
        return Meter(id: id, title: "Power",
                     major: watts.map { "\(Int($0.rounded())) W" } ?? "—",
                     minor: temp.map { "\(Int($0.rounded())) °C" } ?? "—",
                     minorColor: color,
                     outer: watts.map { [Segment(id: 0, from: 0, to: $0 / maxW, color: $0 > tdp ? Palette.hot : Palette.cool)] } ?? [],
                     inner: temp.map { [Segment(id: 0, from: 0, to: thermal.fraction($0), color: color)] } ?? [])
    }

    private func ramMeter(id: Int) -> Meter {
        guard let ram = sys?.ram, ram.total > 0 else {
            return Meter(id: id, title: "RAM", major: "—", minor: "—", outer: [])
        }
        let used = min(1, ram.used / ram.total)
        let fluent = min(used, ram.fluent / ram.total)
        let usedColor: Color = used > 0.9 ? .red : .gray.opacity(0.6)
        return Meter(id: id, title: "RAM",
                     major: "\(Int((used * 100).rounded())) %",
                     minor: "Fluent \(fixed(ram.fluent / 1e9, 1)) GB",
                     minorColor: Palette.fluent,
                     outer: [Segment(id: 0, from: 0, to: fluent, color: Palette.fluent),
                             Segment(id: 1, from: fluent, to: used, color: usedColor)])
    }
}
