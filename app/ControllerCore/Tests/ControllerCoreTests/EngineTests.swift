import XCTest
@testable import ControllerCore

final class EngineTests: XCTestCase {
    func testRulesAreHardCoded() {
        XCTAssertEqual(RiskRules.riskFraction, D("0.05"))
        XCTAssertEqual(RiskRules.leverage, 20)
        XCTAssertEqual(RiskRules.marginMode, "CROSSED")
    }

    func testLongSizing() {
        // 余额 1000 → 风险 50；距离 100000-99000=1000 → 0.05 BTC
        let d = RiskEngine.evaluate(side: "LONG", stop: "99000", mark: 100000, balance: 1000, filters: btcFilters)
        XCTAssertTrue(d.passed)
        XCTAssertEqual(d.riskUsd, 50)
        XCTAssertEqual(d.quantity, D("0.05"))
        XCTAssertEqual(d.notional, 5000)
        XCTAssertEqual(d.leverage, 20)
        XCTAssertEqual(d.marginMode, "CROSSED")
    }

    func testQuantityRoundsDownNeverUp() {
        // 50 / 700 = 0.0714… → 0.071
        let d = RiskEngine.evaluate(side: "LONG", stop: "99300", mark: 100000, balance: 1000, filters: btcFilters)
        XCTAssertEqual(d.quantity, D("0.071"))
        XCTAssertLessThanOrEqual(d.quantity! * 700, 50)
    }

    func testShortStopAlignedUp() {
        let d = RiskEngine.evaluate(side: "SHORT", stop: "101000.03", mark: 100000, balance: 1000, filters: btcFilters)
        XCTAssertEqual(d.stop, D("101000.1"))
        XCTAssertTrue(d.passed)
    }

    func testLongStopAlignedDown() {
        XCTAssertEqual(RiskEngine.alignStop(D("98999.99"), tick: D("0.1"), side: .long), D("98999.9"))
    }

    func testMissingStopRejected() {
        for s in [nil, "", "   "] as [String?] {
            let d = RiskEngine.evaluate(side: "LONG", stop: s, mark: 100, balance: 1000, filters: btcFilters)
            XCTAssertEqual(d.reasonCode, "MISSING_STOP")
        }
    }

    func testInvalidStop() {
        for s in ["abc", "0", "-1", "1abc", "NaN", "inf"] {
            XCTAssertEqual(RiskEngine.evaluate(side: "LONG", stop: s, mark: 100, balance: 1000, filters: btcFilters).reasonCode, "INVALID_STOP", s)
        }
    }

    func testWrongSideStops() {
        XCTAssertEqual(RiskEngine.evaluate(side: "LONG", stop: "101", mark: 100, balance: 1000, filters: btcFilters).reasonCode, "STOP_NOT_BELOW_MARK")
        XCTAssertEqual(RiskEngine.evaluate(side: "LONG", stop: "100", mark: 100, balance: 1000, filters: btcFilters).reasonCode, "STOP_NOT_BELOW_MARK")
        XCTAssertEqual(RiskEngine.evaluate(side: "SHORT", stop: "99", mark: 100, balance: 1000, filters: btcFilters).reasonCode, "STOP_NOT_ABOVE_MARK")
    }

    func testSideRequired() {
        XCTAssertEqual(RiskEngine.evaluate(side: nil, stop: "99", mark: 100, balance: 1000, filters: btcFilters).reasonCode, "INVALID_SIDE")
        XCTAssertEqual(RiskEngine.evaluate(side: "up", stop: "99", mark: 100, balance: 1000, filters: btcFilters).reasonCode, "INVALID_SIDE")
    }

    func testMinQtyAndNotional() {
        // 风险 50 / 距离 60000 = 0.00083 → 0 → 低于最小量
        XCTAssertEqual(RiskEngine.evaluate(side: "LONG", stop: "40000", mark: 100000, balance: 1000, filters: btcFilters).reasonCode, "QTY_BELOW_MIN")
        // 余额 20 → 风险 1；距离 1000 → 0.001 BTC；名义 100 刚好；mark 99999 → 名义 99.999 < 100
        XCTAssertEqual(RiskEngine.evaluate(side: "LONG", stop: "98999", mark: 99999, balance: 20, filters: btcFilters).reasonCode, "NOTIONAL_BELOW_MIN")
        XCTAssertTrue(RiskEngine.evaluate(side: "LONG", stop: "99000", mark: 100000, balance: 20, filters: btcFilters).passed)
    }

    func testInvalidInputs() {
        XCTAssertEqual(RiskEngine.evaluate(side: "LONG", stop: "99", mark: nil, balance: 1000, filters: btcFilters).reasonCode, "INVALID_INPUT")
        XCTAssertEqual(RiskEngine.evaluate(side: "LONG", stop: "99", mark: 100, balance: -1, filters: btcFilters).reasonCode, "INVALID_INPUT")
        XCTAssertEqual(RiskEngine.evaluate(side: "LONG", stop: "99", mark: 100, balance: 0, filters: btcFilters).reasonCode, "INVALID_INPUT")
        XCTAssertEqual(RiskEngine.evaluate(side: "LONG", stop: "99", mark: 100, balance: 1000, filters: nil).reasonCode, "INVALID_INPUT")
        XCTAssertNil(SymbolFilters(tickSize: 0, stepSize: 1, minQty: 1, minNotional: 1))
    }

    func testWatchlistGate() {
        XCTAssertNil(RiskEngine.requireInWatchlist("btcusdt", ["BTCUSDT"]))
        XCTAssertEqual(RiskEngine.requireInWatchlist("DOGEUSDT", ["BTCUSDT"])?.reasonCode, "NOT_IN_WATCHLIST")
        XCTAssertEqual(RiskEngine.requireInWatchlist("BTCUSDT", nil)?.reasonCode, "NOT_IN_WATCHLIST")
        XCTAssertEqual(RiskEngine.requireInWatchlist("", ["BTCUSDT"])?.reasonCode, "NOT_IN_WATCHLIST")
    }

    func testDecimalFormatting() {
        XCTAssertEqual(Dec.str(D("1.500")), "1.5")
        XCTAssertEqual(Dec.str(D("100")), "100")
        XCTAssertEqual(Dec.str(D("0.00000001")), "0.00000001")
        XCTAssertEqual(Dec.str(D("-0.0")), "0")
        XCTAssertNil(Dec.parse("1abc"))
        XCTAssertNil(Dec.parse("1,5"))
        XCTAssertEqual(Dec.parse("1e-3"), D("0.001"))
        XCTAssertEqual(Dec.roundHalfEven(D("0.000000125"), scale: 8), D("0.00000012"))
        XCTAssertEqual(Dec.roundHalfEven(D("0.000000135"), scale: 8), D("0.00000014"))
    }

    func testSigningOfficialVector() {
        // 币安官方文档示例
        let sig = Signer.hmacSHA256Hex(secret: "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j",
                                       message: "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559")
        XCTAssertEqual(sig, "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71")
    }

    func testSigningEdgeCases() {
        // 期望值由 Python hmac 计算：长 Key（>64 字节先哈希）、空消息、长消息
        XCTAssertEqual(Signer.hmacSHA256Hex(secret: String(repeating: "k", count: 100), message: ""),
                       "d46761768ee3c864fc76c08b3d73c8092853ae3fefa01b990963dfa2d56e7334")
        XCTAssertEqual(Signer.hmacSHA256Hex(secret: "abc", message: String(repeating: "x", count: 200)),
                       "f2ceb233554898886ab7862256c888b0708e3d8fac37a3ec5532896a6786f4dd")
    }

    func testSignedQueryShape() {
        let q = Signer.signedQuery([("symbol", "BTCUSDT"), ("side", "BUY")], secret: "s", timestampMs: 1, recvWindow: 5000)
        XCTAssertTrue(q.hasPrefix("symbol=BTCUSDT&side=BUY&timestamp=1&recvWindow=5000&signature="))
        let body = String(q[..<q.range(of: "&signature=")!.lowerBound])
        XCTAssertEqual(q.components(separatedBy: "signature=").last, Signer.hmacSHA256Hex(secret: "s", message: body))
        XCTAssertEqual(Signer.encode("a b/中"), "a+b%2F%E4%B8%AD")
    }
}
