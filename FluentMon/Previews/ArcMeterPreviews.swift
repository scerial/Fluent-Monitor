import SwiftUI

private struct ArcMeterCases: View {
    var body: some View {
        VStack(spacing: 24) {
            ForEach([100.0, 120.0, 156.0], id: \.self) { diameter in
                HStack(spacing: 24) {
                    ArcMeter(meter: SampleData.meters[0], diameter: diameter)
                    ArcMeter(meter: SampleData.meters[1], diameter: diameter)
                    ArcMeter(meter: SampleData.meters[2], diameter: diameter)
                    ArcMeter(meter: SampleData.meters[3], diameter: diameter)
                }
            }
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(white: 0.06))
        .preferredColorScheme(.dark)
    }
}

#Preview("ArcMeter 11-inch") {
    ArcMeterCases()
        .frame(width: 1194, height: 834)
}

#Preview("ArcMeter 13-inch") {
    ArcMeterCases()
        .frame(width: 1366, height: 1024)
}
