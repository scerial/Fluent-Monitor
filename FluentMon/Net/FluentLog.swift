import Foundation

struct FluentLog {
    static let columns = ["continuity", "x-velocity", "y-velocity", "z-velocity", "k", "omega"]

    struct Row {
        let iter: Int
        let values: [Double]
    }

    /// Solver state as it stood when a row was read, so any prefix of the run can be replayed.
    struct Progress {
        let stepStart: Int
        let step: Int
        let stepsLeft: Int
        let flowTime: Double
        let lineCount: Int
        /// Lowest value the least converged residual has reached so far, and which column it is.
        let best: Double
        let bestColumn: Int
    }

    private(set) var rows: [Row] = []
    private(set) var progress: [Progress] = []
    private(set) var converged: [Int] = []
    private(set) var lines: [(text: String, kind: Character)] = []
    private(set) var firstStep = 0

    init(text: String) {
        var seen = Set<Int>()
        var pendingStepStart = false
        var stepStartRow = 0
        var currentStep = 0
        var stepsLeft = 0
        var flowTime = 0.0
        var best = Double.infinity
        var bestColumn = 0
        for substring in text.split(whereSeparator: \.isNewline) {
            let line = substring.trimmingCharacters(in: .whitespaces)
            if line.isEmpty { continue }
            let tokens = line.split(separator: " ")
            if let row = Self.dataRow(tokens) {
                if !seen.contains(row.iter) {
                    seen.insert(row.iter)
                    if pendingStepStart {
                        stepStartRow = rows.count
                        pendingStepStart = false
                    }
                    rows.append(row)
                    lines.append((line, "r"))
                    let worst = row.values.max() ?? .infinity
                    if worst < best {
                        best = worst
                        bestColumn = row.values.firstIndex(of: worst) ?? 0
                    }
                    progress.append(Progress(stepStart: stepStartRow, step: currentStep, stepsLeft: stepsLeft,
                                             flowTime: flowTime, lineCount: lines.count,
                                             best: best, bestColumn: bestColumn))
                } else {
                    lines.append((line, "r"))
                }
            } else if line.hasPrefix("!"), line.hasSuffix("solution is converged") {
                if let iter = tokens.dropFirst().first.flatMap({ Int($0) }) { converged.append(iter) }
                lines.append((line, "n"))
            } else if line.hasPrefix("Flow time = ") {
                pendingStepStart = true
                let parts = line.components(separatedBy: ", time step = ")
                if parts.count == 2 {
                    let seconds = parts[0].dropFirst("Flow time = ".count).dropLast()
                    flowTime = Double(seconds) ?? flowTime
                    if let step = Int(parts[1].trimmingCharacters(in: .whitespaces)) {
                        if firstStep == 0 { firstStep = step }
                        currentStep = step
                    }
                }
                lines.append((line, "s"))
            } else if line.hasSuffix("more time steps"), let count = tokens.first.flatMap({ Int($0) }) {
                stepsLeft = count
                lines.append((line, "s"))
            } else if line.hasPrefix("Reversed flow") {
                lines.append((line, "w"))
            } else if line.hasPrefix("Error") {
                lines.append((line, "e"))
            } else {
                lines.append((line, "n"))
            }
        }
    }

    static func fromDefaults() -> FluentLog? {
        guard let path = UserDefaults.standard.string(forKey: "mockLog") else { return nil }
        guard let data = FileManager.default.contents(atPath: path) else {
            print("mockLog: cannot read \(path)")
            return nil
        }
        return FluentLog(text: String(decoding: data, as: UTF8.self))
    }

    /// Snapshot of the first `count` rows, with the console tail up to the same point.
    func snapshot(upTo count: Int) -> [String: Any] {
        let end = min(count, rows.count)
        let lineEnd = end > 0 ? progress[end - 1].lineCount : 0
        let tailStart = max(0, lineEnd - 500)
        var message: [String: Any] = [
            "t": "snapshot",
            "session": ["id": "log", "case": "orifice dp0 FFF", "mode": "transient",
                        "status": "running", "started": ""],
            "columns": Self.columns,
            "kinds": Self.columns.map { _ in "res" },
            "iters": rows[0..<end].map { $0.iter },
            "values": Self.columns.indices.map { column in rows[0..<end].map { $0.values[column] } },
            "con_tail": ["first": tailStart + 1,
                         "lines": lines[tailStart..<lineEnd].map { $0.text },
                         "kinds": String(lines[tailStart..<lineEnd].map { $0.kind })]
        ]
        if let bars = bars(at: end - 1) {
            message["bars"] = bars
        }
        return message
    }

    /// Console lines produced by rows `range`, including the non-data lines that follow them.
    func console(rows range: Range<Int>) -> [String: Any]? {
        guard !range.isEmpty else { return nil }
        let lower = range.lowerBound > 0 ? progress[range.lowerBound - 1].lineCount : 0
        let upper = progress[range.upperBound - 1].lineCount
        return console(lines: lower..<upper)
    }

    /// Console lines after the last data row, sent once the replay reaches the end of the run.
    func trailingConsole() -> [String: Any]? {
        let lower = progress.last?.lineCount ?? 0
        return console(lines: lower..<lines.count)
    }

    private func console(lines range: Range<Int>) -> [String: Any]? {
        guard !range.isEmpty else { return nil }
        let slice = lines[range]
        return ["t": "con", "first": range.lowerBound + 1,
                "lines": slice.map { $0.text }, "kinds": String(slice.map { $0.kind })]
    }

    func bars(at index: Int) -> [String: Any]? {
        guard rows.indices.contains(index) else { return nil }
        let state = progress[index]
        let reference = rows[0].values.max() ?? state.best
        let rmax = state.best
        let convSpan = reference > 0 ? log10(reference) + 6 : 6
        let decades = rmax > 0 && reference > 0 ? max(0, log10(reference) - log10(rmax)) : 0
        let limiting = state.bestColumn
        let currentStep = state.step
        let stepsLeft = state.stepsLeft
        let flowTime = state.flowTime
        let total = currentStep + stepsLeft
        let span = max(1, total - firstStep)
        let done = max(0, currentStep - firstStep)
        let stepSize = currentStep > 0 ? flowTime / Double(currentStep) : 0
        let endTime = flowTime + Double(stepsLeft) * stepSize
        return [
            "iter": ["p": min(1, Double(done) / Double(span)), "done": done, "total": span,
                     "left": stepsLeft, "label": "Time step \(currentStep) of \(total)"],
            "conv": ["p": min(1, decades / convSpan), "rmax": rmax, "limiting": Self.columns[limiting],
                     "decades": decades, "of": convSpan, "state": "ok"],
            "step": ["p": endTime > 0 ? min(1, flowTime / endTime) : 0, "n": currentStep,
                     "left": stepsLeft, "flow_time": flowTime, "t_end": endTime,
                     "label": String(format: "t = %.6f s", flowTime)]
        ]
    }

    private static func dataRow(_ tokens: [Substring]) -> Row? {
        guard tokens.count == 9, let iter = Int(tokens[0]), tokens[7].contains(":"),
              Int(tokens[8]) != nil else { return nil }
        let values = tokens[1...6].compactMap { Double($0) }
        guard values.count == 6 else { return nil }
        return Row(iter: iter, values: values)
    }
}
