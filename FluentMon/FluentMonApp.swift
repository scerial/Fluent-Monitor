import SwiftUI

@MainActor
@Observable
final class AppModel {
    let store = MonitorStore()
    let discovery = Discovery()
    var feed: (any Feed)?
    var showConnect = false
    var showSettings = false
    var tokenRejected = false
    var pendingEndpoint: Endpoint?

    init() {
        if UserDefaults.standard.bool(forKey: "mock") {
            startMock()
        } else if let saved = Self.savedEndpoint() {
            connect(saved)
        } else {
            showConnect = true
        }
    }

    func connect(_ endpoint: Endpoint) {
        feed?.stop()
        Self.save(endpoint)
        let client = MonitorClient(store: store, endpoint: endpoint)
        client.onRejected = { [weak self] in
            self?.tokenRejected = true
            self?.showConnect = true
        }
        feed = client
        tokenRejected = false
        pendingEndpoint = nil
        showConnect = false
        client.start()
    }

    func startMock() {
        feed?.stop()
        let mock = MockClient(store: store)
        feed = mock
        mock.start()
    }

    func resume() {
        (feed as? MonitorClient)?.resumeIfNeeded()
    }

    func open(_ url: URL) {
        guard let endpoint = PairingURL.parse(url) else { return }
        pendingEndpoint = endpoint
        showConnect = true
    }

    func subscribeConsole(_ on: Bool) {
        feed?.send(.sub(console: on))
    }

    func loadOlder(before: Int) {
        feed?.send(.conBack(before: before, n: 500))
    }

    static func savedEndpoint() -> Endpoint? {
        let defaults = UserDefaults.standard
        guard let host = defaults.string(forKey: "fluentmon.host"), !host.isEmpty,
              let token = Keychain.get(account: "token") else { return nil }
        var components = URLComponents()
        components.scheme = defaults.bool(forKey: "fluentmon.wss") ? "wss" : "ws"
        components.host = host
        components.port = defaults.object(forKey: "fluentmon.port") as? Int ?? 8765
        components.path = "/ws"
        guard let url = components.url else { return nil }
        return Endpoint(url: url, token: token)
    }

    static func save(_ endpoint: Endpoint) {
        let defaults = UserDefaults.standard
        defaults.set(endpoint.url.host() ?? "", forKey: "fluentmon.host")
        defaults.set(endpoint.url.port ?? 8765, forKey: "fluentmon.port")
        defaults.set(endpoint.url.scheme == "wss", forKey: "fluentmon.wss")
        Keychain.set(endpoint.token, account: "token")
    }
}

@main
struct FluentMonApp: App {
    @State private var model = AppModel()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            RootView(model: model)
                .environment(model.store)
                .preferredColorScheme(.dark)
                .onChange(of: scenePhase) { _, phase in
                    if phase == .active {
                        model.resume()
                    }
                }
        }
    }
}

struct RootView: View {
    @Bindable var model: AppModel
    @Environment(MonitorStore.self) private var store

    var body: some View {
        DashboardView(
            onSubscribe: { model.subscribeConsole($0) },
            onLoadOlder: { model.loadOlder(before: $0) },
            onConnect: { model.showConnect = true },
            onSettings: { model.showSettings = true }
        )
        .onOpenURL { model.open($0) }
        .onChange(of: model.showConnect) { _, open in
            if open {
                model.discovery.start()
            } else {
                model.discovery.stop()
            }
        }
        .sheet(isPresented: $model.showConnect) {
            ConnectSheet(
                servers: model.discovery.servers,
                onConnect: { model.connect($0) },
                initial: model.pendingEndpoint ?? AppModel.savedEndpoint(),
                discovery: model.discovery.state,
                tokenRejected: model.tokenRejected
            )
        }
        .sheet(isPresented: $model.showSettings) {
            SettingsSheet(
                settings: Binding(get: { store.settings }, set: { store.settings = $0 }),
                residualNames: store.series.map(\.name)
            )
        }
    }
}
