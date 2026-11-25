import SwiftUI

struct SettingsSheet: View {
    @Binding var settings: ClientSettings
    var residualNames: [String] = []

    @State private var cpuMessage: String?

    private var exponent: Int {
        min(8, max(3, Int((-log10(settings.target)).rounded())))
    }

    private var duplicateMessage: String? {
        let counts = Dictionary(grouping: settings.meterCpus, by: { $0 })
        guard let cpu = counts.first(where: { $0.value.count > 1 })?.key else { return nil }
        return "CPU \(cpu) is on two meters. Choose a different CPU for one of them."
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Residual target") {
                    LabeledContent("Target") {
                        HStack(spacing: 12) {
                            Text("1e-\(exponent)")
                                .font(.body.monospacedDigit())
                            Stepper("", value: exponentBinding, in: 3...8)
                                .labelsHidden()
                        }
                    }
                }
                Section("Exclude from chart") {
                    if residualNames.isEmpty {
                        Text("No residuals in the current run")
                            .foregroundStyle(.secondary)
                    }
                    ForEach(residualNames, id: \.self) { name in
                        Toggle(name, isOn: excludeBinding(name))
                    }
                }
                Section {
                    ForEach(0..<settings.meterCpus.count, id: \.self) { index in
                        Picker("Meter \(index + 1)", selection: cpuBinding(index)) {
                            ForEach(0...15, id: \.self) { cpu in
                                Text("CPU \(cpu)").tag(cpu)
                            }
                        }
                    }
                    if let message = cpuMessage ?? duplicateMessage {
                        Text(message)
                            .foregroundStyle(.red)
                    }
                } header: {
                    Text("Meter CPUs")
                } footer: {
                    Text("These changes affect this iPad only. No wire message sends them to the server yet.")
                }
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
        }
        .dynamicTypeSize(...DynamicTypeSize.xLarge)
        .onAppear {
            settings.target = pow(10, -Double(exponent))
        }
    }

    private var exponentBinding: Binding<Int> {
        Binding(
            get: { exponent },
            set: { settings.target = pow(10, -Double($0)) }
        )
    }

    private func excludeBinding(_ name: String) -> Binding<Bool> {
        Binding(
            get: { settings.exclude.contains(name) },
            set: { excluded in
                if excluded {
                    if !settings.exclude.contains(name) {
                        settings.exclude.append(name)
                    }
                } else {
                    settings.exclude.removeAll { $0 == name }
                }
            }
        )
    }

    private func cpuBinding(_ index: Int) -> Binding<Int> {
        Binding(
            get: { settings.meterCpus[index] },
            set: { cpu in
                let taken = settings.meterCpus.enumerated().contains { $0.offset != index && $0.element == cpu }
                if taken {
                    cpuMessage = "CPU \(cpu) is already on another meter."
                } else {
                    settings.meterCpus[index] = cpu
                    cpuMessage = nil
                }
            }
        )
    }
}
