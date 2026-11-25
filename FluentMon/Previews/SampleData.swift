import Foundation
import SwiftUI

enum SampleData {
    static let residualNames = ["continuity", "x-velocity", "y-velocity", "z-velocity", "energy", "k", "omega", "epsilon"]

    static let session = Session(caseName: "duct_kw", mode: .transient, status: .running)

    static let meters: [Meter] = [
        Meter(id: 1, title: "CPU 4", major: "97 %", minor: "4.9 GHz",
              outer: [Segment(id: 0, from: 0, to: 0.97, color: Palette.cool)]),
        Meter(id: 4, title: "Power", major: "71 W", minor: "68 °C", minorColor: Palette.cool,
              outer: [Segment(id: 0, from: 0, to: 71 / 200, color: Palette.cool)],
              inner: [Segment(id: 0, from: 0, to: (68 - 20) / (90 - 20), color: Palette.cool)]),
        Meter(id: 5, title: "RAM", major: "62 %", minor: "Fluent 12.0 GB", minorColor: Palette.fluent,
              outer: [Segment(id: 0, from: 0, to: 0.25, color: Palette.fluent),
                      Segment(id: 1, from: 0.25, to: 0.62, color: .gray.opacity(0.6))]),
        Meter(id: 9, title: "CPU 8", major: "—", minor: "—", outer: [])
    ]

    @MainActor
    static func store(names: Int, iterations: Int = 1500, target: Double = 1e-6) -> MonitorStore {
        let store = MonitorStore()
        store.settings.target = target
        let columns = Array(residualNames.prefix(names))
        let iters = Array(1...iterations)
        let values: [[Double]] = columns.indices.map { index in
            let start = pow(10, Double(-1 - index % 3))
            let tau = 120 + Double(index) * 30
            return iters.map { start * exp(-Double($0) / tau) + 1e-8 }
        }
        let object: [String: Any] = [
            "t": "snapshot",
            "session": ["id": "preview", "case": "duct_kw", "mode": "transient", "status": "running", "started": ""],
            "columns": columns,
            "kinds": columns.map { _ in "res" },
            "iters": iters,
            "values": values
        ]
        if let data = try? JSONSerialization.data(withJSONObject: object),
           let message = try? decodeIncoming(data) {
            store.apply(message, now: .now)
        }
        return store
    }

    static func consoleLines(count: Int) -> [ConsoleLine] {
        let messages: [(String, Character)] = [
            ("Reading case duct_kw.cas.h5", "h"),
            ("iter 120  continuity 2.31e-03", "r"),
            ("Warning: negative static pressure in 3 cells", "w"),
            ("iter 121  continuity 2.18e-03", "r"),
            ("Time step 14 complete", "s"),
            ("Error: divergence detected in k", "e"),
            ("Writing data file", "n")
        ]
        return (0..<count).map { i in
            let message = messages[i % messages.count]
            return ConsoleLine(id: i + 1, text: message.0, kind: message.1)
        }
    }

    static let servers: [DiscoveredServer] = [
        DiscoveredServer(name: "YOUNGMIN-PC", host: "192.168.1.24", port: 8765),
        DiscoveredServer(name: "Fluent workstation in the north lab with a very long display name", host: "youngmin-pc.tailnet.ts.net", port: 8765)
    ]

    static let endpoint = Endpoint(url: URL(string: "ws://192.168.1.24:8765/ws")!, token: "sample-token")
}
