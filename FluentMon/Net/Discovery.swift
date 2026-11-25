import Foundation
import Network
import Observation

struct DiscoveredServer: Identifiable, Hashable {
    var id: String { "\(host):\(port)" }
    let name: String
    let host: String
    let port: Int
}

enum DiscoveryState: Equatable {
    case searching, found, notFound, denied
}

enum PairingURL {
    static func parse(_ url: URL) -> Endpoint? {
        guard url.scheme == "fluentmon", url.host() == "connect" else { return nil }
        let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        func value(_ name: String) -> String? {
            items.first { $0.name == name }?.value
        }
        guard let host = value("host"), !host.isEmpty,
              let token = value("token"), !token.isEmpty else { return nil }
        let port = value("port").flatMap(Int.init) ?? 8765
        guard (1...65535).contains(port) else { return nil }
        var components = URLComponents()
        components.scheme = "ws"
        components.host = host
        components.port = port
        components.path = "/ws"
        guard let url = components.url else { return nil }
        return Endpoint(url: url, token: token)
    }
}

@Observable
@MainActor
final class Discovery {
    private(set) var servers: [DiscoveredServer] = []
    private(set) var state: DiscoveryState = .searching

    private var browser: NWBrowser?
    private var connections: [NWConnection] = []

    func start() {
        guard browser == nil else { return }
        servers = []
        state = .searching
        let browser = NWBrowser(for: .bonjour(type: "_fluentmon._tcp", domain: nil), using: .tcp)
        browser.stateUpdateHandler = { [weak self] newState in
            Task { @MainActor [weak self] in
                self?.browserChanged(newState)
            }
        }
        browser.browseResultsChangedHandler = { [weak self] results, _ in
            Task { @MainActor [weak self] in
                self?.resultsChanged(results)
            }
        }
        browser.start(queue: .main)
        self.browser = browser
    }

    func stop() {
        browser?.cancel()
        browser = nil
        connections.forEach { $0.cancel() }
        connections = []
    }

    private func browserChanged(_ newState: NWBrowser.State) {
        if case .failed = newState {
            state = .denied
        }
    }

    private func resultsChanged(_ results: Set<NWBrowser.Result>) {
        if results.isEmpty {
            servers = []
            state = .notFound
            return
        }
        for result in results {
            guard case .service(let name, _, _, _) = result.endpoint else { continue }
            resolve(name: name, endpoint: result.endpoint)
        }
    }

    private func resolve(name: String, endpoint: NWEndpoint) {
        let connection = NWConnection(to: endpoint, using: .tcp)
        connections.append(connection)
        connection.stateUpdateHandler = { [weak self] newState in
            guard case .ready = newState else { return }
            guard let remote = connection.currentPath?.remoteEndpoint,
                  case .hostPort(let host, let port) = remote else { return }
            let hostName = "\(host)".components(separatedBy: "%").first ?? "\(host)"
            Task { @MainActor [weak self] in
                self?.added(DiscoveredServer(name: name, host: hostName, port: Int(port.rawValue)))
                connection.cancel()
            }
        }
        connection.start(queue: .main)
    }

    private func added(_ server: DiscoveredServer) {
        if !servers.contains(server) {
            servers.append(server)
        }
        state = servers.isEmpty ? .notFound : .found
    }
}
