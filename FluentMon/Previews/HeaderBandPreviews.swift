import SwiftUI

private struct HeaderBandCases: View {
    private let now = Date()

    var body: some View {
        VStack(spacing: 12) {
            HeaderBand(session: Session(caseName: "duct_kw", mode: .transient, status: .running),
                       connection: .live, lastIteration: now.addingTimeInterval(-0.4))
            HeaderBand(session: Session(caseName: "duct_kw", mode: .steady, status: .converged),
                       connection: .live, lastIteration: now.addingTimeInterval(-12))
            HeaderBand(session: Session(caseName: "FFF", mode: .steady, status: .idle),
                       connection: .live, lastIteration: now.addingTimeInterval(-185))
            HeaderBand(session: Session(caseName: "mixer_v2", mode: .transient, status: .problem),
                       connection: .live, lastIteration: now.addingTimeInterval(-3900))
            HeaderBand(session: Session(caseName: "duct_kw", mode: .transient, status: .running),
                       connection: .offline, lastIteration: now.addingTimeInterval(-30))
            HeaderBand(session: Session(caseName: "Turbine_blade_rotor_stage_3_with_a_very_long_name_for_testing", mode: .transient, status: .running),
                       connection: .live, lastIteration: now.addingTimeInterval(-1))
        }
        .frame(height: 32 * 6 + 12 * 5)
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(white: 0.06))
        .preferredColorScheme(.dark)
    }
}

#Preview("HeaderBand 11-inch") {
    HeaderBandCases()
        .frame(width: 1194, height: 834)
}

#Preview("HeaderBand 13-inch") {
    HeaderBandCases()
        .frame(width: 1366, height: 1024)
}
