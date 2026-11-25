import XCTest
@testable import FluentMon

final class KeychainTests: XCTestCase {
    func testSetGetDelete() {
        let account = "test-\(UUID().uuidString)"
        defer { Keychain.delete(account: account) }
        XCTAssertNil(Keychain.get(account: account))
        XCTAssertTrue(Keychain.set("first", account: account))
        XCTAssertEqual(Keychain.get(account: account), "first")
        XCTAssertTrue(Keychain.set("second", account: account))
        XCTAssertEqual(Keychain.get(account: account), "second")
        Keychain.delete(account: account)
        XCTAssertNil(Keychain.get(account: account))
    }
}
