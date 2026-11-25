import Combine
import SwiftUI

struct DashboardView: View {
    @Environment(MonitorStore.self) private var store
    @Environment(\.displayScale) private var displayScale
    @ScaledMetric(relativeTo: .body) private var textScale: CGFloat = 1
    @State private var scene = SceneState()
    @State private var mode: DashboardGeometry.Mode = .landscape

    let onSubscribe: (Bool) -> Void
    let onLoadOlder: (Int) -> Void
    let onConnect: () -> Void
    let onSettings: () -> Void

    var body: some View {
        GeometryReader { proxy in
            if proxy.size.width < 640 || proxy.size.height < 480 {
                Text("Enlarge the window")
                    .font(.headline)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                content(size: proxy.size)
                    .onChange(of: proxy.size, initial: true) { _, size in
                        mode = LayoutMode.resolve(size: size, isFullScreen: scene.isFullScreen,
                                                  orientation: scene.orientation, previous: mode)
                    }
                    .onChange(of: scene) { _, s in
                        mode = LayoutMode.resolve(size: proxy.size, isFullScreen: s.isFullScreen,
                                                  orientation: s.orientation, previous: mode)
                    }
            }
        }
        .background(SceneProbe(state: $scene))
        .background(Color(white: 0.06))
        .onReceive(Timer.publish(every: 1, on: .main, in: .common).autoconnect()) { now in
            store.refreshConnection(now: now)
        }
    }

    private func content(size: CGSize) -> some View {
        let full = BarMetrics(scale: textScale)
        let first = DashboardGeometry(size: size, mode: mode, barsHeight: full.height, displayScale: displayScale)
        let compact = first.chart.height < 300
        let metrics = compact ? full.compact : full
        let geo = compact
            ? DashboardGeometry(size: size, mode: mode, barsHeight: metrics.height, displayScale: displayScale)
            : first
        let live = store.connection == .live
        let dimmed = live ? 1.0 : 0.5

        return ZStack(alignment: .topLeading) {
            HeaderBand(session: store.session, connection: store.connection, lastIteration: store.lastIterationAt)
                .padding(.trailing, 76)
                .place(geo.header)
            HStack(spacing: 8) {
                Button(action: onConnect) {
                    Image(systemName: "wifi")
                }
                .accessibilityLabel("Connection")
                Button(action: onSettings) {
                    Image(systemName: "gearshape")
                }
                .accessibilityLabel("Settings")
            }
            .frame(width: geo.header.width, height: geo.header.height, alignment: .trailing)
            .place(geo.header)
            ResidualChart(store: store)
                .place(geo.chart)
                .opacity(dimmed)
            ProgressBars(bars: store.bars, metrics: metrics)
                .place(geo.bars)
                .opacity(dimmed)
            ForEach(Array(store.meters.enumerated()), id: \.offset) { index, meter in
                if index < geo.meters.count {
                    ArcMeter(meter: meter, diameter: geo.gaugeDiameter)
                        .place(geo.meters[index])
                        .saturation(live ? 1 : 0)
                        .opacity(live ? 1 : 0.5)
                }
            }
            ConsoleDrawer(console: store.console, onSubscribe: onSubscribe, onLoadOlder: onLoadOlder)
                .place(geo.chart.union(geo.bars))
        }
        .frame(width: size.width, height: size.height, alignment: .topLeading)
        .animation(.snappy(duration: 0.35), value: geo)
    }
}
