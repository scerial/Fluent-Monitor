import XCTest
@testable import FluentMon

final class PairingURLTests: XCTestCase {
    func testParsesPairingLink() throws {
        let url = try XCTUnwrap(URL(string: "fluentmon://connect?host=youngmin-pc.tailnet.ts.net&port=9000&token=abc"))
        let endpoint = try XCTUnwrap(PairingURL.parse(url))
        XCTAssertEqual(endpoint.url.absoluteString, "ws://youngmin-pc.tailnet.ts.net:9000/ws")
        XCTAssertEqual(endpoint.token, "abc")
    }

    func testDefaultPort() throws {
        let url = try XCTUnwrap(URL(string: "fluentmon://connect?host=192.168.1.24&token=abc"))
        XCTAssertEqual(PairingURL.parse(url)?.url.port, 8765)
    }

    func testRejectsMissingTokenBadPortAndOtherSchemes() throws {
        XCTAssertNil(PairingURL.parse(try XCTUnwrap(URL(string: "fluentmon://connect?host=h"))))
        XCTAssertNil(PairingURL.parse(try XCTUnwrap(URL(string: "fluentmon://connect?host=h&token=t&port=0"))))
        XCTAssertNil(PairingURL.parse(try XCTUnwrap(URL(string: "fluentmon://other?host=h&token=t"))))
        XCTAssertNil(PairingURL.parse(try XCTUnwrap(URL(string: "https://connect?host=h&token=t"))))
    }
}
