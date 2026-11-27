import Foundation

@MainActor
final class MockClient: Feed {
    private let store: MonitorStore
    private var timer: Timer?
    private var nextIter = 1
    private var nextLine = 1
    private var convergedSent = false
    private let log: FluentLog?
    private var replayNext = 0

    static let columns = ["continuity", "x-velocity", "y-velocity", "z-velocity", "energy", "k", "omega", "cd-1"]
    static let kinds = ["res", "res", "res", "res", "res", "res", "res", "rep"]
    static let starts: [Double] = [1, 0.12, 0.1, 0.11, 0.003, 0.4, 0.9, 0.81]
    static let taus: [Double] = [260, 220, 230, 240, 300, 200, 180, 400]
    static let totalIters = 1000

    init(store: MonitorStore) {
        self.store = store
        self.log = FluentLog.fromDefaults()
    }

    func start() {
        guard timer == nil else { return }
        store.setOffline(false)
        feed(["t": "hello", "proto": 1, "server": "MOCK", "version": "0.0.0",
              "thermal": ["base_c": 20, "warn_c": 70, "max_c": 89]])
        if log != nil {
            feed(log?.snapshot(upTo: 0) ?? [:])
            feed(["t": "event", "kind": "run_start", "iter": 0, "text": "run started", "ts": ""])
            timer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
                Task { @MainActor [weak self] in
                    self?.replayStep()
                }
            }
            return
        }
        feed(snapshot())
        feed(["t": "event", "kind": "run_start", "iter": 0, "text": "run started", "ts": ""])
        timer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.step()
            }
        }
    }

    func stop() {
        timer?.invalidate()
        timer = nil
        store.setOffline(true)
    }

    func send(_ message: Outgoing) {
        guard case .conBack(let before, let count) = message else { return }
        let first = max(1, before - count)
        guard first < before else { return }
        let lines = (first..<before).map { "    \($0)  2.2900e-03  ...  0:12:38  516" }
        feed(["t": "con", "first": first, "lines": lines, "kinds": String(repeating: "r", count: lines.count)])
    }

    private func replayStep() {
        guard let log else { return }
        if replayNext < log.rows.count {
            let end = min(log.rows.count, replayNext + 24)
            let rows = log.rows[replayNext..<end].map { [$0.iter, $0.values] as [Any] }
            feed(["t": "resid", "rows": rows])
            if let bars = log.bars(at: end - 1) {
                feed(["t": "bars"].merging(bars) { $1 })
            }
            if let console = log.console(rows: replayNext..<end) {
                feed(console)
            }
            replayNext = end
            return
        }
        if let tail = log.trailingConsole() {
            feed(tail)
        }
        feed(["t": "event", "kind": "session_end", "iter": log.rows.last?.iter ?? 0,
              "text": "Calculation complete", "ts": ""])
        timer?.invalidate()
        timer = nil
    }

    private func feed(_ object: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: object),
              let message = try? decodeIncoming(data) else { return }
        store.apply(message, now: .now)
    }

    private func value(column: Int, iter: Int) -> Double {
        Self.starts[column] * exp(-Double(iter) / Self.taus[column]) + 1e-8
    }

    private func snapshot() -> [String: Any] {
        let iters = Array(1...50)
        let values = (0..<Self.columns.count).map { c in iters.map { value(column: c, iter: $0) } }
        nextIter = 51
        return [
            "t": "snapshot",
            "session": ["id": "mock", "case": "duct_kw", "mode": "transient", "status": "running", "started": ""],
            "columns": Self.columns,
            "kinds": Self.kinds,
            "iters": iters,
            "values": values,
            "bars": bars(iter: 50),
            "con_tail": ["first": 1, "lines": ["Reading case duct_kw.cas.h5"], "kinds": "h"]
        ]
    }

    private func bars(iter: Int) -> [String: Any] {
        let p = min(1, Double(iter) / Double(Self.totalIters))
        let steady = iter % 4 == 0
        var step: Any = NSNull()
        if !steady {
            step = ["p": min(1, Double(iter) / 800), "n": iter, "left": max(0, 800 - iter),
                    "flow_time": Double(iter) * 0.0001, "t_end": 0.1, "eta_s": 9000,
                    "label": "t = \(String(format: "%.4f", Double(iter) * 0.0001)) s"]
        }
        return [
            "iter": ["p": p, "done": iter, "total": Self.totalIters, "left": Self.totalIters - iter,
                     "eta_s": Double(Self.totalIters - iter) * 2.6, "label": "Iteration \(iter)"],
            "conv": ["p": min(1, Double(iter) / 1500), "rmax": value(column: 0, iter: iter),
                     "limiting": "continuity", "decades": 3.4, "of": 6.0, "iters_to_target": 1900,
                     "state": "ok"],
            "step": step
        ]
    }

    private func step() {
        let iter = nextIter
        nextIter += 1
        let values = (0..<Self.columns.count).map { c in value(column: c, iter: iter) }
        feed(["t": "resid", "rows": [[iter, values]]])
        feed(["t": "bars"].merging(bars(iter: iter)) { $1 })
        feed(["t": "con", "first": nextLine,
              "lines": ["   \(iter)  \(String(format: "%.4e", values[0]))  ...  0:12:38  \(Self.totalIters - iter)",
                        "Time step \(iter / 4) complete"],
              "kinds": "rs"])
        nextLine += 2
        if iter == 412, !convergedSent {
            convergedSent = true
            feed(["t": "event", "kind": "converged", "iter": iter, "text": "solution is converged", "ts": ""])
        }
        let t = Double(iter)
        feed(["t": "sys", "cpu_ids": [0, 4, 8, 12],
              "cpu": [60 + 30 * sin(t / 7), 70 + 25 * sin(t / 5), 80 + 15 * sin(t / 9), 65 + 30 * sin(t / 6)],
              "pkg_w": 90 + 25 * sin(t / 4), "pkg_max_w": 162, "tdp_w": 120,
              "clk_mhz": [4950, 4925, 4975, 4900], "tctl_c": 70 + 20 * sin(t / 11),
              "ram": ["total": 34359738368.0, "used": 21474836480.0 + 1e9 * sin(t / 13),
                      "fluent": 12884901888.0]])
    }
}
