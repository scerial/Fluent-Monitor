import SwiftUI

struct ProgressBars: View {
    let bars: Bars?
    let metrics: BarMetrics

    var body: some View {
        VStack(spacing: metrics.rowGap) {
            row(title: "Iterations",
                p: bars?.iter.p,
                fill: bars?.converged == true ? .green : Palette.cool,
                dimmed: false,
                captionText: bars?.iter.label,
                ticks: nil)
            row(title: "Convergence to 1e-6",
                p: bars?.conv.p,
                fill: Self.convColor(bars?.conv.state),
                dimmed: false,
                captionText: bars?.conv.label,
                ticks: bars?.conv.of)
            row(title: "Time steps",
                p: bars?.step?.p,
                fill: .purple,
                dimmed: bars?.step == nil,
                captionText: bars?.step == nil ? "Steady run" : bars?.step?.label,
                ticks: nil)
        }
        .padding(.vertical, metrics.padV)
        .padding(.horizontal, 16)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .dynamicTypeSize(...DynamicTypeSize.xLarge)
    }

    private func row(title: String, p: Double?, fill: Color,
                     dimmed: Bool, captionText: String?, ticks: Double?) -> some View {
        VStack(alignment: .leading, spacing: 8 * metrics.scale) {
            Text(title)
                .font(.system(size: 17 * metrics.scale, weight: .semibold))
                .lineLimit(1)
            track(p: p ?? 0, fill: fill, ticks: ticks)
            if metrics.showCaptions {
                Text(captionText ?? " ")
                    .font(.system(size: 14 * metrics.scale))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
        }
        .opacity(dimmed ? 0.4 : 1)
        .frame(height: metrics.row)
    }

    private func track(p: Double, fill: Color, ticks: Double?) -> some View {
        let clamped = min(1, max(0, p))
        return GeometryReader { proxy in
            ZStack(alignment: .leading) {
                Capsule()
                    .fill(.white.opacity(0.12))
                Capsule()
                    .fill(fill)
                    .frame(width: proxy.size.width * clamped)
                    .animation(.easeOut(duration: 0.3), value: clamped)
                if let of = ticks, of > 1 {
                    ForEach(Array(1..<max(1, Int(ceil(of)))), id: \.self) { i in
                        Rectangle()
                            .fill(.white.opacity(0.3))
                            .frame(width: 1, height: 12 * metrics.scale + 3)
                            .offset(x: proxy.size.width * CGFloat(Double(i) / of))
                    }
                }
            }
        }
        .frame(height: 12 * metrics.scale)
    }

    private static func convColor(_ state: ConvState?) -> Color {
        switch state {
        case .ok: return .teal
        case .stalled: return .orange
        case .rising, .nan: return .red
        case nil: return .gray
        }
    }
}
