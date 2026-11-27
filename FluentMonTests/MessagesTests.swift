import XCTest
@testable import FluentMon

final class MessagesTests: XCTestCase {
    private func decode(_ json: String) throws -> Incoming? {
        try decodeIncoming(Data(json.utf8))
    }

    func testHelloThermal() throws {
        guard case .hello(let hello)? = try decode(#"{"t":"hello","proto":1,"server":"S","version":"0.1.0","thermal":{"base_c":20,"warn_c":70,"max_c":89}}"#) else {
            return XCTFail("not hello")
        }
        XCTAssertEqual(hello.thermal, Thermal(baseC: 20, warnC: 70, maxC: 89))
    }

    func testSnapshotWithNullValuesAndCaseKey() throws {
        let json = #"{"t":"snapshot","session":{"id":"x","case":"duct_kw","mode":"steady","status":"running","started":""},"columns":["continuity","cd-1"],"kinds":["res","rep"],"iters":[1,2],"values":[[1.0,null],[0.5,0.25]],"bars":null,"con_tail":{"first":9735,"lines":["a"],"kinds":"h"}}"#
        guard case .snapshot(let snap)? = try decode(json) else { return XCTFail("not snapshot") }
        XCTAssertEqual(snap.session.caseName, "duct_kw")
        XCTAssertNil(snap.values[0][1])
        XCTAssertEqual(snap.conTail?.first, 9735)
        XCTAssertNil(snap.bars)
    }

    func testResidRowWithNullValue() throws {
        guard case .resid(let resid)? = try decode(#"{"t":"resid","rows":[[483,[2.31e-3,null]]]}"#) else {
            return XCTFail("not resid")
        }
        XCTAssertEqual(resid.rows[0].iter, 483)
        XCTAssertNil(resid.rows[0].values[1])
    }

    func testBarsWithNullStepAndSysWithNullClock() throws {
        let bars = #"{"t":"bars","iter":{"p":0.5,"done":5,"total":10,"left":5,"eta_s":10,"label":"x"},"conv":{"p":0.5,"rmax":0.001,"limiting":"continuity","decades":3.4,"of":6.0,"iters_to_target":100,"state":"ok"},"step":null}"#
        guard case .bars(let b)? = try decode(bars) else { return XCTFail("not bars") }
        XCTAssertNil(b.step)
        XCTAssertEqual(b.conv?.state, "ok")

        let sys = #"{"t":"sys","cpu_ids":[0],"cpu":[50.0],"pkg_w":null,"pkg_max_w":162,"tdp_w":120,"clk_mhz":[null],"tctl_c":null,"ram":{"total":100,"used":50,"fluent":10}}"#
        guard case .sys(let s)? = try decode(sys) else { return XCTFail("not sys") }
        XCTAssertNil(s.pkgW)
        XCTAssertNil(s.clkMhz[0])
        XCTAssertEqual(s.ram.fluent, 10)
    }

    func testSysClockDecimalAndNull() throws {
        let sys = #"{"t":"sys","cpu_ids":[0,4],"cpu":[50.0,60.0],"pkg_w":null,"pkg_max_w":162,"tdp_w":120,"clk_mhz":[890.5,null],"tctl_c":null,"ram":{"total":100,"used":50,"fluent":10}}"#
        guard case .sys(let s)? = try decode(sys) else { return XCTFail("not sys") }
        XCTAssertEqual(s.clkMhz[0], 890.5)
        XCTAssertNil(s.clkMhz[1])
    }

    func testBarsWithAllNullIterConvStep() throws {
        guard case .bars(let b)? = try decode(#"{"t":"bars","iter":null,"conv":null,"step":null}"#) else {
            return XCTFail("not bars")
        }
        XCTAssertNil(b.iter)
        XCTAssertNil(b.conv)
        XCTAssertNil(b.step)
    }

    func testStepWithNullTEnd() throws {
        let bars = #"{"t":"bars","iter":null,"conv":null,"step":{"p":0.1,"n":120,"left":880,"flow_time":0.012,"t_end":null,"eta_s":null,"label":"x"}}"#
        guard case .bars(let b)? = try decode(bars) else { return XCTFail("not bars") }
        XCTAssertNil(b.step?.tEnd)
        XCTAssertEqual(b.step?.flowTime, 0.012)
    }

    func testUnknownTypeIsSkipped() throws {
        XCTAssertNil(try decode(#"{"t":"future_thing"}"#))
    }

    func testMalformedFrameThrows() {
        XCTAssertThrowsError(try decode("not json"))
    }

    func testOutgoingEncoding() throws {
        let sub = try JSONSerialization.jsonObject(with: Outgoing.sub(console: true).data) as? [String: Any]
        XCTAssertEqual(sub?["t"] as? String, "sub")
        XCTAssertEqual(sub?["console"] as? Bool, true)
        let back = try JSONSerialization.jsonObject(with: Outgoing.conBack(before: 9735, n: 500).data) as? [String: Any]
        XCTAssertEqual(back?["before"] as? Int, 9735)
        XCTAssertEqual(back?["n"] as? Int, 500)
    }
}
