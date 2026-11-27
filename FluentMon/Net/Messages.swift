import Foundation

struct Thermal: Decodable, Equatable {
    let baseC: Double
    let warnC: Double
    let maxC: Double
}

struct HelloMsg: Decodable {
    let proto: Int
    let server: String
    let version: String
    let session: String?
    let thermal: Thermal
}

struct SessionInfo: Decodable {
    let id: String
    let caseName: String
    let mode: String
    let status: String
    let started: String?

    enum CodingKeys: String, CodingKey {
        case id, mode, status, started
        case caseName = "case"
    }
}

struct IterBar: Decodable, Equatable {
    let p: Double
    let done: Int
    let total: Int
    let left: Int
    let etaS: Double?
    let label: String
}

struct ConvBar: Decodable, Equatable {
    let p: Double
    let rmax: Double?
    let limiting: String?
    let decades: Double?
    let of: Double
    let itersToTarget: Int?
    let state: String
}

struct StepBar: Decodable, Equatable {
    let p: Double
    let n: Int
    let left: Int
    let flowTime: Double
    let tEnd: Double?
    let etaS: Double?
    let label: String
}

struct BarsMsg: Decodable, Equatable {
    let iter: IterBar?
    let conv: ConvBar?
    let step: StepBar?
}

struct SnapshotMsg: Decodable {
    let session: SessionInfo
    let columns: [String]
    let kinds: [String]
    let iters: [Int]
    let values: [[Double?]]
    let bars: BarsMsg?
    let conTail: ConMsg?
}

struct ResidRow: Decodable, Equatable {
    let iter: Int
    let values: [Double?]

    init(from decoder: Decoder) throws {
        var container = try decoder.unkeyedContainer()
        iter = try container.decode(Int.self)
        values = try container.decode([Double?].self)
    }
}

struct ResidMsg: Decodable {
    let rows: [ResidRow]
}

struct RamMsg: Decodable, Equatable {
    let total: Double
    let used: Double
    let fluent: Double
}

struct SysMsg: Decodable, Equatable {
    let cpuIds: [Int]
    let cpu: [Double?]
    let pkgW: Double?
    let pkgMaxW: Double?
    let tdpW: Double?
    let clkMhz: [Double?]
    let tctlC: Double?
    let ram: RamMsg
}

struct ConMsg: Decodable, Equatable {
    let first: Int
    let lines: [String]
    let kinds: String
}

struct EventMsg: Decodable, Equatable {
    let kind: String
    let iter: Int?
    let text: String?
    let ts: String?
}

enum Incoming {
    case hello(HelloMsg)
    case snapshot(SnapshotMsg)
    case resid(ResidMsg)
    case bars(BarsMsg)
    case sys(SysMsg)
    case con(ConMsg)
    case event(EventMsg)
}

enum Outgoing: Equatable {
    case sub(console: Bool)
    case conBack(before: Int, n: Int)
    case ping

    var data: Data {
        let object: [String: Any]
        switch self {
        case .sub(let console): object = ["t": "sub", "console": console]
        case .conBack(let before, let n): object = ["t": "con_back", "before": before, "n": n]
        case .ping: object = ["t": "ping"]
        }
        return (try? JSONSerialization.data(withJSONObject: object)) ?? Data()
    }
}

private struct Envelope: Decodable {
    let t: String
}

func decodeIncoming(_ data: Data) throws -> Incoming? {
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    let type = try decoder.decode(Envelope.self, from: data).t
    switch type {
    case "hello": return .hello(try decoder.decode(HelloMsg.self, from: data))
    case "snapshot": return .snapshot(try decoder.decode(SnapshotMsg.self, from: data))
    case "resid": return .resid(try decoder.decode(ResidMsg.self, from: data))
    case "bars": return .bars(try decoder.decode(BarsMsg.self, from: data))
    case "sys": return .sys(try decoder.decode(SysMsg.self, from: data))
    case "con": return .con(try decoder.decode(ConMsg.self, from: data))
    case "event": return .event(try decoder.decode(EventMsg.self, from: data))
    default: return nil
    }
}
