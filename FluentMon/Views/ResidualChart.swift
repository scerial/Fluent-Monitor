import Charts
import SwiftUI

struct ResidualChart: View {
    let store: MonitorStore
    @State private var range = XRange.all

    private var names: [String] { Array(store.names.prefix(8)) }

    private var series: [Series] {
        store.visibleSeries(range: range).filter { names.contains($0.name) }
    }

    private var scrub: Binding<Int?> {
        Binding(get: { store.scrubIter }, set: { store.scrubIter = $0 })
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 12) {
                Picker("Range", selection: $range) {
                    ForEach(XRange.allCases) { Text($0.title).tag($0) }
                }
                .pickerStyle(.segmented)
                .frame(width: 280)
                readout
            }
            if store.series.isEmpty {
                Text("Waiting for a Fluent run")
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                chart
            }
        }
    }

    private var chart: some View {
        Chart {
            ForEach(series) { s in
                ForEach(s.points) { p in
                    LineMark(x: .value("Iteration", p.iter),
                             y: .value("Residual", p.value),
                             series: .value("Equation", s.name))
                        .foregroundStyle(by: .value("Equation", s.name))
                        .lineStyle(StrokeStyle(lineWidth: 1.5))
                }
            }
            RuleMark(y: .value("Target", store.target))
                .lineStyle(StrokeStyle(lineWidth: 2.5, dash: [8, 5]))
                .foregroundStyle(.white.opacity(0.75))
        }
        .chartYScale(domain: store.yDomain, type: .log)
        .chartXScale(domain: store.xDomain(range: range))
        .chartForegroundStyleScale(domain: names, range: Palette.residuals)
        .chartLegend(position: .overlay, alignment: .topTrailing, spacing: 8)
        .chartXSelection(value: scrub)
        .font(.system(size: 11))
    }

    @ViewBuilder
    private var readout: some View {
        if let iter = store.scrubIter {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 12) {
                    Text("iter \(iter)")
                        .foregroundStyle(.secondary)
                    ForEach(store.series.filter { names.contains($0.name) }) { s in
                        Text("\(s.name) \(Self.format(value(of: s, at: iter)))")
                            .foregroundStyle(color(for: s.name))
                    }
                }
                .font(.system(size: 11, design: .monospaced))
            }
        }
    }

    private func value(of s: Series, at iter: Int) -> Double? {
        s.points.min { abs($0.iter - iter) < abs($1.iter - iter) }?.value
    }

    private func color(for name: String) -> Color {
        let i = names.firstIndex(of: name) ?? 0
        return Palette.residuals[i % Palette.residuals.count]
    }

    private static func format(_ value: Double?) -> String {
        guard let value else { return "—" }
        return String(format: "%.2e", value)
    }
}

private extension XRange {
    var title: String {
        switch self {
        case .all: return "All"
        case .last500: return "Last 500"
        case .last100: return "Last 100"
        }
    }
}
