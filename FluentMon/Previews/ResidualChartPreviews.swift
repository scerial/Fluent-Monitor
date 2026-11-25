import SwiftUI

private struct ResidualChartCases: View {
    let withReadout: Bool

    var body: some View {
        VStack(spacing: 24) {
            ResidualChart(store: store(names: 3))
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            ResidualChart(store: store(names: 0))
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(white: 0.06))
        .preferredColorScheme(.dark)
    }

    private func store(names: Int) -> MonitorStore {
        let series = SampleData.series(names: Array(SampleData.residualNames.prefix(names)), iterations: 1500)
        let store = MonitorStore(series: series, target: 1e-6)
        if withReadout {
            store.scrubIter = 800
        }
        return store
    }
}

#Preview("ResidualChart 11-inch") {
    ResidualChartCases(withReadout: false)
        .frame(width: 1033, height: 448)
}

#Preview("ResidualChart 13-inch") {
    ResidualChartCases(withReadout: false)
        .frame(width: 1183, height: 646)
}

#Preview("ResidualChart readout 11-inch") {
    ResidualChartCases(withReadout: true)
        .frame(width: 1033, height: 448)
}
