import SwiftUI

private struct SettingsPreviewHost: View {
    @State var settings: ClientSettings

    var body: some View {
        SettingsSheet(settings: $settings, residualNames: SampleData.residualNames)
            .preferredColorScheme(.dark)
    }
}

private struct SettingsCases: View {
    var body: some View {
        HStack(spacing: 24) {
            SettingsPreviewHost(settings: ClientSettings())
            SettingsPreviewHost(settings: ClientSettings(target: 1e-4, exclude: ["epsilon", "omega"], meterCpus: [0, 1, 2, 3]))
            SettingsPreviewHost(settings: ClientSettings(target: 1e-6, exclude: [], meterCpus: [4, 4, 8, 12]))
        }
    }
}

#Preview("SettingsSheet 11-inch") {
    SettingsCases()
        .frame(width: 1194, height: 834)
}

#Preview("SettingsSheet 13-inch") {
    SettingsCases()
        .frame(width: 1366, height: 1024)
}
