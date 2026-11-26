import SwiftUI

struct ConnectSheet: View {
    let servers: [DiscoveredServer]
    let onConnect: (Endpoint) -> Void
    let initial: Endpoint?
    var discovery: DiscoveryState = .found
    var tokenRejected = false

    @State private var host = ""
    @State private var portText = "8765"
    @State private var useWss = false
    @State private var token = ""
    @State private var showToken = false
    @FocusState private var tokenFocused: Bool

    var body: some View {
        NavigationStack {
            Form {
                if tokenRejected {
                    Section {
                        Text("The token was rejected. Check it and try again.")
                            .foregroundStyle(.red)
                    }
                }
                Section("Nearby servers") {
                    discoveredRows
                }
                Section {
                    TextField("Host", text: $host)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                        .onChange(of: host) { _, new in
                            let cleaned = Self.clean(new)
                            host = cleaned.host
                            if cleaned.secure {
                                useWss = true
                            }
                        }
                    TextField("Port", text: $portText)
                        .keyboardType(.numberPad)
                    if !portValid {
                        Text("Port must be 1 to 65535")
                            .foregroundStyle(.red)
                    }
                    Toggle("Use wss", isOn: $useWss)
                } header: {
                    Text("Server")
                } footer: {
                    Text("Campus Wi-Fi often hides servers; enter the address by hand.")
                }
                Section("Token") {
                    HStack {
                        Group {
                            if showToken {
                                TextField("Token", text: $token)
                            } else {
                                SecureField("Token", text: $token)
                            }
                        }
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .focused($tokenFocused)
                        Button(showToken ? "Hide" : "Show") {
                            showToken.toggle()
                        }
                        .buttonStyle(.borderless)
                    }
                }
                Section {
                    connectButton
                }
            }
            .navigationTitle("Connect to FluentMon")
            .navigationBarTitleDisplayMode(.inline)
        }
        .dynamicTypeSize(...DynamicTypeSize.xLarge)
        .onAppear {
            if let initial {
                host = initial.url.host() ?? ""
                portText = "\(initial.url.port ?? 8765)"
                useWss = initial.url.scheme == "wss"
                token = initial.token
            }
            if tokenRejected {
                tokenFocused = true
            }
        }
    }

    @ViewBuilder
    private var connectButton: some View {
        if #available(iOS 26.0, *) {
            Button("Connect") {
                connect()
            }
            .buttonStyle(.glassProminent)
            .frame(maxWidth: .infinity)
            .disabled(!canConnect)
        } else {
            Button("Connect") {
                connect()
            }
            .buttonStyle(.borderedProminent)
            .frame(maxWidth: .infinity)
            .disabled(!canConnect)
        }
    }

    @ViewBuilder
    private var discoveredRows: some View {
        switch discovery {
        case .searching:
            HStack(spacing: 10) {
                ProgressView()
                Text("Searching…")
            }
            serverRows
        case .found:
            serverRows
        case .notFound:
            Text("No servers found nearby")
                .foregroundStyle(.secondary)
        case .denied:
            Text("FluentMon cannot look for servers. Turn on Local Network for FluentMon in Settings, then reopen this sheet.")
                .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private var serverRows: some View {
        if servers.isEmpty {
            if discovery == .found {
                Text("No servers found nearby")
                    .foregroundStyle(.secondary)
            }
        } else {
            ForEach(servers) { server in
                Button {
                    host = server.host
                    portText = "\(server.port)"
                    useWss = false
                } label: {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(server.name)
                            .lineLimit(1)
                            .truncationMode(.middle)
                        Text("\(server.host):\(server.port)")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }
                }
            }
        }
    }

    private var port: Int? {
        guard let value = Int(portText), (1...65535).contains(value) else { return nil }
        return value
    }

    private var portValid: Bool {
        port != nil || portText.isEmpty
    }

    private var canConnect: Bool {
        !host.isEmpty && !token.isEmpty && port != nil
    }

    private func connect() {
        guard let port, !host.isEmpty, !token.isEmpty else { return }
        var components = URLComponents()
        components.scheme = useWss ? "wss" : "ws"
        components.host = host
        components.port = port
        components.path = "/ws"
        guard let url = components.url else { return }
        onConnect(Endpoint(url: url, token: token))
    }

    static func clean(_ raw: String) -> (host: String, secure: Bool) {
        var value = raw.trimmingCharacters(in: .whitespaces)
        var secure = false
        for (prefix, isSecure) in [("wss://", true), ("ws://", false)] where value.lowercased().hasPrefix(prefix) {
            value.removeFirst(prefix.count)
            secure = isSecure
        }
        if let slash = value.firstIndex(of: "/") {
            value = String(value[..<slash])
        }
        return (value, secure)
    }
}
