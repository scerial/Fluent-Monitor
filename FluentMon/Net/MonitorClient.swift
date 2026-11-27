import Foundation
import os
import UIKit

private let netLog = Logger(subsystem: "dev.local.fluentmon", category: "net")

struct Endpoint: Equatable {
    var url: URL
    var token: String
}

@MainActor
protocol Feed: AnyObject {
    func start()
    func stop()
    func send(_ message: Outgoing)
}

@MainActor
final class MonitorClient: Feed {
    static let backoff: [TimeInterval] = [0.5, 1, 2, 4, 8, 10]
    static let rejectedCode = 4401

    static func delay(attempt: Int) -> TimeInterval {
        backoff[min(max(attempt, 0), backoff.count - 1)]
    }

    var onRejected: (() -> Void)?

    private let store: MonitorStore
    private let endpoint: Endpoint
    private let urlSession = URLSession(configuration: .default)
    private var task: URLSessionWebSocketTask?
    private var attempt = 0
    private var running = false
    private var connected = false
    private var retry: Task<Void, Never>?

    init(store: MonitorStore, endpoint: Endpoint) {
        self.store = store
        self.endpoint = endpoint
    }

    func start() {
        guard !running else { return }
        running = true
        connect()
    }

    func stop() {
        running = false
        retry?.cancel()
        retry = nil
        task?.cancel(with: .normalClosure, reason: nil)
        task = nil
        connected = false
        store.setOffline(true)
        UIApplication.shared.isIdleTimerDisabled = false
    }

    func send(_ message: Outgoing) {
        task?.send(.data(message.data)) { _ in }
    }

    func subscribeConsole(_ on: Bool) {
        send(.sub(console: on))
    }

    func requestOlder(before: Int, count: Int = 500) {
        send(.conBack(before: before, n: count))
    }

    func resumeIfNeeded() {
        guard running, !connected else { return }
        retry?.cancel()
        retry = nil
        connect()
    }

    private func connect() {
        retry = nil
        var request = URLRequest(url: endpoint.url)
        request.setValue("Bearer \(endpoint.token)", forHTTPHeaderField: "Authorization")
        let socket = urlSession.webSocketTask(with: request)
        task = socket
        socket.resume()
        receive(on: socket)
    }

    nonisolated private func receive(on socket: URLSessionWebSocketTask) {
        socket.receive { [weak self] result in
            switch result {
            case .success(let message):
                var decoded: Incoming?
                if case .data(let data) = message {
                    do {
                        decoded = try decodeIncoming(data)
                    } catch {
                        netLog.error("decode failed: \(String(describing: error), privacy: .public)")
                    }
                }
                Task { @MainActor [weak self] in
                    self?.handle(socket, decoded)
                }
                self?.receive(on: socket)
            case .failure:
                Task { @MainActor [weak self] in
                    self?.disconnected(socket)
                }
            }
        }
    }

    private func handle(_ socket: URLSessionWebSocketTask, _ message: Incoming?) {
        guard socket === task else { return }
        if !connected {
            connected = true
            store.setOffline(false)
            UIApplication.shared.isIdleTimerDisabled = true
        }
        guard let message else { return }
        if case .snapshot = message {
            attempt = 0
        }
        store.apply(message, now: .now)
    }

    private func disconnected(_ socket: URLSessionWebSocketTask) {
        guard socket === task else { return }
        task = nil
        connected = false
        store.setOffline(true)
        UIApplication.shared.isIdleTimerDisabled = false
        if socket.closeCode.rawValue == Self.rejectedCode {
            running = false
            onRejected?()
            return
        }
        guard running else { return }
        let delay = Self.delay(attempt: attempt)
        attempt += 1
        retry = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            guard !Task.isCancelled else { return }
            self?.connect()
        }
    }
}
