import SwiftUI

struct ArcMeter: View {
    let meter: Meter
    let diameter: CGFloat
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    private let sweep = 0.75
    private var lw: CGFloat { max(8, 0.085 * diameter) }
    private var lwInner: CGFloat { max(5, 0.045 * diameter) }
    private var innerInset: CGFloat { lw + 0.03 * diameter }

    var body: some View {
        ZStack {
            ring(0, 1, .white.opacity(0.10), width: lw, inset: 0)
            ForEach(meter.outer) { s in
                ring(s.from, s.to, s.color, width: lw, inset: 0)
            }
            if !meter.inner.isEmpty {
                ring(0, 1, .white.opacity(0.10), width: lwInner, inset: innerInset)
                ForEach(meter.inner) { s in
                    ring(s.from, s.to, s.color, width: lwInner, inset: innerInset)
                }
            }
            VStack(spacing: 0.02 * diameter) {
                Text(meter.title)
                    .font(.system(size: max(10, 0.075 * diameter), weight: .light))
                    .foregroundStyle(.secondary)
                Text(meter.major)
                    .font(.system(size: max(10, 0.17 * diameter), weight: .regular, design: .rounded).monospacedDigit())
                if let minor = meter.minor {
                    Text(minor)
                        .font(.system(size: max(10, 0.085 * diameter), weight: .regular).monospacedDigit())
                        .foregroundStyle(meter.minorColor)
                }
            }
            .lineLimit(1)
            .minimumScaleFactor(0.8)
            .frame(maxWidth: 0.6 * diameter)
        }
        .frame(width: diameter, height: diameter)
        .frame(height: DashboardGeometry.k * diameter, alignment: .top)
    }

    private func ring(_ a: Double, _ b: Double, _ c: Color, width: CGFloat, inset: CGFloat) -> some View {
        let lo = min(1, max(0, a))
        let hi = min(1, max(lo, b))
        return Circle()
            .inset(by: inset + width / 2)
            .trim(from: lo * sweep, to: hi * sweep)
            .stroke(c, style: StrokeStyle(lineWidth: width, lineCap: .butt))
            .rotationEffect(.degrees(135))
            .animation(reduceMotion ? nil : .linear(duration: 0.2), value: hi)
    }
}
