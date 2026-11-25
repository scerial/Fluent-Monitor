import XCTest
@testable import FluentMon

@MainActor
final class MonitorStoreTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_000_000)

    private func apply(_ store: MonitorStore, _ json: String, at date: Date? = nil) throws {
        guard let message = try decodeIncoming(Data(json.utf8)) else { return XCTFail("unknown") }
        store.apply(message, now: date ?? now)
    }

    private func snapshot(store: MonitorStore) throws {
        try apply(store, #"{"t":"snapshot","session":{"id":"x","case":"FFF","mode":"steady","status":"running","started":""},"columns":["continuity","x-velocity","cd-1"],"kinds":["res","res","rep"],"iters":[1,2,3],"values":[[1.0,0.5,0.25],[0.3,null,0.1],[9,9,9]],"bars":null,"con_tail":{"first":1,"lines":["header"],"kinds":"h"}}"#)
    }

    func testSnapshotBuildsResidualSeriesOnly() throws {
        let store = MonitorStore()
        try snapshot(store: store)
        XCTAssertEqual(store.names, ["continuity", "x-velocity"])
        XCTAssertEqual(store.series[1].points.map(\.iter), [1, 3])
        XCTAssertEqual(store.session?.status, .running)
        XCTAssertEqual(store.console.lines.count, 1)
        XCTAssertEqual(store.lastIterationAt, now)
    }

    func testResidAppendsAndIgnoresDuplicates() throws {
        let store = MonitorStore()
        try snapshot(store: store)
        try apply(store, #"{"t":"resid","rows":[[3,[0.2,0.1,0.0]],[4,[0.1,0.05,0.01]]]}"#)
        XCTAssertEqual(store.series[0].points.map(\.iter), [1, 2, 3, 4])
        XCTAssertEqual(store.series[1].points.map(\.iter), [1, 3, 4])
    }

    func testSysBuildsMetersAndSmoothsCPU() throws {
        let store = MonitorStore()
        store.settings.meterCpus = [0, 4, 8, 12]
        try apply(store, #"{"t":"sys","cpu_ids":[0,4,8,12],"cpu":[100,100,100,100],"pkg_w":71.4,"pkg_max_w":162,"tdp_w":120,"clk_mhz":[4950,null,4975,4900],"tctl_c":68.0,"ram":{"total":100,"used":62,"fluent":12}}"#)
        try apply(store, #"{"t":"sys","cpu_ids":[0,4,8,12],"cpu":[0,100,100,100],"pkg_w":71.4,"pkg_max_w":162,"tdp_w":120,"clk_mhz":[4950,null,4975,4900],"tctl_c":68.0,"ram":{"total":100,"used":62,"fluent":12}}"#)
        XCTAssertEqual(store.cpuSmoothed[0], 50)
        let meters = store.meters
        XCTAssertEqual(meters.count, 6)
        XCTAssertEqual(meters[0].major, "50 %")
        XCTAssertEqual(meters[0].minor, "5.0 GHz")
        XCTAssertEqual(meters[4].minor, "68 °C")
        XCTAssertEqual(meters[4].major, "71 W")
        XCTAssertEqual(meters[5].major, "62 %")
        XCTAssertEqual(meters[5].minor, "Fluent 0.0 GB")
    }

    func testMissingPowerSensorShowsDash() throws {
        let store = MonitorStore()
        try apply(store, #"{"t":"sys","cpu_ids":[0],"cpu":[10],"pkg_w":null,"pkg_max_w":162,"tdp_w":120,"clk_mhz":[4000],"tctl_c":null,"ram":{"total":100,"used":10,"fluent":1}}"#)
        XCTAssertEqual(store.meters[1].major, "—")
        XCTAssertTrue(store.meters[1].outer.isEmpty)
    }

    func testEventChangesStatusAndBarsMapping() throws {
        let store = MonitorStore()
        try snapshot(store: store)
        try apply(store, #"{"t":"event","kind":"converged","iter":3,"text":"x","ts":""}"#)
        XCTAssertEqual(store.session?.status, .converged)
        try apply(store, #"{"t":"bars","iter":{"p":0.5,"done":5,"total":10,"left":518,"eta_s":758,"label":"x"},"conv":{"p":0.57,"rmax":0.0023,"limiting":"continuity","decades":3.4,"of":6.0,"iters_to_target":1900,"state":"ok"},"step":null}"#)
        XCTAssertEqual(store.bars?.iter.label, "518 left · ETA 0:12:38")
        XCTAssertEqual(store.bars?.conv.state, .ok)
        XCTAssertEqual(store.bars?.converged, true)
        XCTAssertNil(store.bars?.step)
    }

    func testConnectionStaleAfterSevenSeconds() throws {
        let store = MonitorStore()
        store.setOffline(false)
        try apply(store, #"{"t":"sys","cpu_ids":[],"cpu":[],"pkg_w":null,"pkg_max_w":162,"tdp_w":120,"clk_mhz":[],"tctl_c":null,"ram":{"total":100,"used":10,"fluent":1}}"#)
        store.refreshConnection(now: now.addingTimeInterval(6))
        XCTAssertEqual(store.connection, .live)
        store.refreshConnection(now: now.addingTimeInterval(8))
        XCTAssertEqual(store.connection, .stale)
        store.setOffline(true)
        XCTAssertEqual(store.connection, .offline)
    }

    func testVisibleSeriesDropsNonPositiveAndExcluded() throws {
        let store = MonitorStore()
        try snapshot(store: store)
        store.settings.exclude = ["x-velocity"]
        let visible = store.visibleSeries(range: .all)
        XCTAssertEqual(visible.map(\.name), ["continuity"])
        XCTAssertEqual(visible[0].points.count, 3)
    }
}
