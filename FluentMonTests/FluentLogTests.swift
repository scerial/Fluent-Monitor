import XCTest
@testable import FluentMon

final class FluentLogTests: XCTestCase {
    private let sample = """
    ! 4300 solution is converged
    Flow time = 0.0007229999999999999s, time step = 723
    2284 more time steps

    Updating solution at time level N...
     done.

      iter  continuity  x-velocity  y-velocity  z-velocity           k       omega     time/iter
      4300  9.6559e-04  7.5718e-07  4.4214e-07  4.4161e-07  1.0664e-05  3.1635e-06  0:00:12   20

     Reversed flow on 2727 faces (71.2% area) of pressure-outlet 16.
      4301  1.4746e-03  6.9755e-05  4.4551e-05  4.4498e-05  3.9864e-03  1.3154e-03  0:00:11   19
    ! 4301 solution is converged
    Flow time = 0.0007239999999999999s, time step = 724
    2283 more time steps

      iter  continuity  x-velocity  y-velocity  z-velocity           k       omega     time/iter
      4301  1.4746e-03  6.9755e-05  4.4551e-05  4.4498e-05  3.9864e-03  1.3154e-03  0:00:11   20
      4302  1.5905e-03  2.2122e-05  1.4192e-05  1.4172e-05  8.9121e-04  2.8680e-04  0:00:10   18
    """

    func testParsesRowsOncePerIteration() {
        let log = FluentLog(text: sample)
        XCTAssertEqual(log.rows.map { $0.iter }, [4300, 4301, 4302])
    }

    func testParsesConvergenceAndTimeSteps() {
        let log = FluentLog(text: sample)
        XCTAssertEqual(log.converged, [4300, 4301])
        XCTAssertEqual(log.progress.last?.step, 724)
        XCTAssertEqual(log.firstStep, 723)
        XCTAssertEqual(log.progress.last?.stepsLeft, 2283)
    }

    func testStepStartRowIsFirstRowOfLatestStep() {
        let log = FluentLog(text: sample)
        let start = log.progress[2].stepStart
        XCTAssertEqual(log.rows[start].iter, 4302)
    }

    func testSnapshotDecodesThroughMessageDecoder() throws {
        let log = FluentLog(text: sample)
        let data = try JSONSerialization.data(withJSONObject: log.snapshot(upTo: log.rows.count))
        guard case .snapshot(let snapshot)? = try decodeIncoming(data) else {
            return XCTFail("snapshot did not decode")
        }
        XCTAssertEqual(snapshot.iters, [4300, 4301, 4302])
        XCTAssertEqual(snapshot.columns.count, 6)
        XCTAssertNotNil(snapshot.bars)
    }
}
