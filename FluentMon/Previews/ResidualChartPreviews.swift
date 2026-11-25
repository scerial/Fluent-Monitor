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

    @MainActor
    private func store(names: Int) -> MonitorStore {
        let store = SampleData.store(names: names)
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
