import SwiftUI

@main
struct FluentMonProtoApp: App {
    var body: some Scene {
        WindowGroup {
            Gallery()
                .preferredColorScheme(.dark)
        }
    }
}

struct Gallery: View {
    private let screen = UserDefaults.standard.string(forKey: "screen") ?? "meters"
    private let level = UserDefaults.standard.string(forKey: "level") ?? "collapsed"

    var body: some View {
        ZStack {
            Color(white: 0.06).ignoresSafeArea()
            content
                .padding(16)
        }
    }

    @ViewBuilder
    private var content: some View {
        switch screen {
        case "meters":
            HStack(spacing: 20) {
                ForEach(SampleData.meters) { meter in
                    ArcMeter(meter: meter, diameter: 120)
                }
            }
        case "bars":
            ProgressBars(bars: BarSamples.transient, metrics: BarMetrics())
        case "chart":
            ResidualChart(store: MonitorStore(series: SampleData.series(names: SampleData.residualNames, iterations: 1500), target: 1e-6))
        case "readout":
            let store = MonitorStore(series: SampleData.series(names: SampleData.residualNames, iterations: 1500), target: 1e-6)
            let _ = { store.scrubIter = 800 }()
            ResidualChart(store: store)
        case "header":
            VStack(spacing: 12) {
                HeaderBand(session: SampleData.session, connection: .live, lastIteration: Date().addingTimeInterval(-12))
                HeaderBand(session: Session(caseName: "FFF", mode: .steady, status: .converged), connection: .live, lastIteration: Date().addingTimeInterval(-4000))
                HeaderBand(session: Session(caseName: "Turbine_blade_rotor_stage_3_with_a_very_long_name_for_testing", mode: .transient, status: .problem), connection: .stale, lastIteration: Date().addingTimeInterval(-0.4))
                HeaderBand(session: SampleData.session, connection: .offline, lastIteration: Date().addingTimeInterval(-90))
            }
            .frame(height: 4 * 32 + 3 * 12)
        case "console":
            ConsoleDrawer(console: ConsoleBuffer(lines: SampleData.consoleLines(count: 400)),
                          onSubscribe: { _ in },
                          onLoadOlder: { _ in },
                          initialLevel: Self.level(level))
        case "connect":
            ConnectSheet(servers: SampleData.servers, onConnect: { _ in }, initial: nil, discovery: .found)
        case "connect-denied":
            ConnectSheet(servers: [], onConnect: { _ in }, initial: SampleData.endpoint, discovery: .denied, tokenRejected: true)
        default:
            SettingsSheet(settings: .constant(ClientSettings(target: 1e-6, exclude: ["omega"], meterCpus: [0, 4, 8, 4])),
                          residualNames: SampleData.residualNames)
        }
    }

    private static func level(_ name: String) -> ConsoleDrawer.Level {
        switch name {
        case "half": return .half
        case "full": return .full
        default: return .collapsed
        }
    }
}
