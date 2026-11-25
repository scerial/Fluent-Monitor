import SwiftUI

enum BarSamples {
    static let transient = Bars(iter: BarRow(p: 0.48, label: "518 left · ETA 0:12:38"), converged: false,
                                conv: ConvRow(p: 0.56, of: 6.0, state: .ok, label: "Limiting: continuity 2.3e-03 · 3.4 / 6.0 decades · ≈ 1,900 iters"),
                                step: BarRow(p: 0.12, label: "t = 0.0120 / 0.1000 s · ETA 2:41:05"))
    static let steady = Bars(iter: BarRow(p: 1, label: "Converged at 412 iterations"), converged: true,
                             conv: ConvRow(p: 1, of: 6.0, state: .ok, label: "Limiting: continuity 9.8e-07 · 6.0 / 6.0 decades"),
                             step: nil)
    static let stalled = Bars(iter: BarRow(p: 0.7, label: "300 left · ETA 0:08:02"), converged: false,
                              conv: ConvRow(p: 0.4, of: 3.4, state: .stalled, label: "Limiting: energy 4.1e-04 · 1.4 / 3.4 decades"),
                              step: BarRow(p: 0.6, label: "t = 0.0600 / 0.1000 s"))
    static let rising = Bars(iter: BarRow(p: 0.2, label: "800 left · ETA 0:21:10"), converged: false,
                             conv: ConvRow(p: 0.1, of: 3.4, state: .rising, label: "Limiting: k 1.0e-02 · rising"),
                             step: BarRow(p: 0.3, label: "t = 0.0300 / 0.1000 s"))
    static let nan = Bars(iter: BarRow(p: 0.3, label: "700 left"), converged: false,
                          conv: ConvRow(p: 0, of: 3.4, state: .nan, label: "Residual is NaN"),
                          step: nil)
}

private struct ProgressBarCases: View {
    var body: some View {
        VStack(spacing: 16) {
            HStack(alignment: .top, spacing: 24) {
                ProgressBars(bars: BarSamples.steady, metrics: BarMetrics())
                ProgressBars(bars: BarSamples.transient, metrics: BarMetrics())
            }
            HStack(alignment: .top, spacing: 24) {
                ProgressBars(bars: BarSamples.stalled, metrics: BarMetrics())
                ProgressBars(bars: BarSamples.rising, metrics: BarMetrics())
            }
            HStack(alignment: .top, spacing: 24) {
                ProgressBars(bars: BarSamples.nan, metrics: BarMetrics())
                ProgressBars(bars: BarSamples.transient, metrics: BarMetrics().compact)
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(white: 0.06))
        .preferredColorScheme(.dark)
    }
}

private struct ProgressTickCases: View {
    var body: some View {
        VStack(spacing: 24) {
            ProgressBars(bars: BarSamples.transient, metrics: BarMetrics())
            ProgressBars(bars: Bars(iter: BarRow(p: 0.5, label: "of 6.0"), converged: false,
                                    conv: ConvRow(p: 0.5, of: 6.0, state: .ok, label: "of = 6.0 (five ticks)"),
                                    step: nil), metrics: BarMetrics())
            ProgressBars(bars: Bars(iter: BarRow(p: 0.5, label: "of 3.4"), converged: false,
                                    conv: ConvRow(p: 0.5, of: 3.4, state: .ok, label: "of = 3.4 (three ticks)"),
                                    step: nil), metrics: BarMetrics())
        }
        .padding(16)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(white: 0.06))
        .preferredColorScheme(.dark)
    }
}

#Preview("ProgressBars cases 11-inch") {
    ProgressBarCases()
        .frame(width: 1194, height: 834)
}

#Preview("ProgressBars cases 13-inch") {
    ProgressBarCases()
        .frame(width: 1366, height: 1024)
}

#Preview("ProgressBars ticks 11-inch") {
    ProgressTickCases()
        .frame(width: 1194, height: 834)
}
