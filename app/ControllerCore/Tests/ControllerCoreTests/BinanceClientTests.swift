import XCTest
@testable import ControllerCore

final class BinanceClientTests: XCTestCase {
    func client(_ routes: [String: (Int, String)], mode: TradingMode = .testnet) throws -> (BinanceSignedExchange, FakeTransport) {
        let t = FakeTransport()
        t.routes = routes
        t.routes["/fapi/v1/time"] = t.routes["/fapi/v1/time"] ?? (200, "{\"serverTime\": \(Int64(Date().timeIntervalSince1970 * 1000))}")
        return (try BinanceSignedExchange(mode: mode, apiKey: "KEY123", secret: "SECRET", http: t), t)
    }

    func testRequiresKeys() {
        XCTAssertThrowsError(try BinanceSignedExchange(mode: .live, apiKey: "", secret: "x"))
        XCTAssertThrowsError(try BinanceSignedExchange(mode: .mock, apiKey: "a", secret: "b"))
    }

    func testTestnetHost() throws {
        let (c, _) = try client([:])
        XCTAssertEqual(c.base, "https://demo-fapi.binance.com")
        XCTAssertEqual(Endpoints.base(for: .live), "https://fapi.binance.com")
    }

    func testStopUsesAlgoOrderWithMarkPriceReduceOnly() async throws {
        let (c, t) = try client(["POST /fapi/v1/algoOrder": (200, "{\"algoId\": 777, \"algoStatus\": \"NEW\"}")])
        let r = try await c.placeStop("BTCUSDT", side: .long, stop: D("99000.5"), quantity: D("0.05"))
        XCTAssertEqual(r.orderId, "777")
        let call = t.calls.last!
        XCTAssertEqual(call.method, "POST"); XCTAssertEqual(call.url.path, "/fapi/v1/algoOrder")
        XCTAssertEqual(call.headers["X-MBX-APIKEY"], "KEY123")
        let q = t.query(call)
        XCTAssertEqual(q["algoType"], "CONDITIONAL"); XCTAssertEqual(q["type"], "STOP_MARKET"); XCTAssertEqual(q["side"], "SELL")
        XCTAssertEqual(q["triggerPrice"], "99000.5"); XCTAssertEqual(q["quantity"], "0.05")
        XCTAssertEqual(q["reduceOnly"], "true"); XCTAssertEqual(q["workingType"], "MARK_PRICE")
        // 签名覆盖除 signature 外的完整 query
        let raw = call.url.query!
        let body = String(raw[..<raw.range(of: "&signature=")!.lowerBound])
        XCTAssertEqual(q["signature"], Signer.hmacSHA256Hex(secret: "SECRET", message: body))
        XCTAssertFalse(raw.contains("SECRET"))
    }

    func testEntryIsMarketAndFlattenReduceOnly() async throws {
        let (c, t) = try client(["POST /fapi/v1/order": (200, "{\"orderId\": 1, \"avgPrice\": \"100.5\"}")])
        let r = try await c.marketOpen("BTCUSDT", side: .short, quantity: D("0.1"))
        XCTAssertEqual(r.fillPrice(fallback: 1), D("100.5"))
        var q = t.query(t.calls.last!)
        XCTAssertEqual(q["type"], "MARKET"); XCTAssertEqual(q["side"], "SELL"); XCTAssertNil(q["reduceOnly"])
        _ = try await c.flatten("BTCUSDT", side: .short, quantity: D("0.1"))
        q = t.query(t.calls.last!)
        XCTAssertEqual(q["side"], "BUY"); XCTAssertEqual(q["reduceOnly"], "true")
    }

    func testAccountSettingsCrossed20x() async throws {
        let (c, t) = try client([
            "GET /fapi/v1/positionSide/dual": (200, "{\"dualSidePosition\": false}"),
            "POST /fapi/v1/marginType": (400, "{\"code\": -4046, \"msg\": \"No need to change margin type.\"}"),
            "POST /fapi/v1/leverage": (200, "{\"leverage\": 20, \"symbol\": \"BTCUSDT\"}"),
        ])
        try await c.ensureAccountSettings("BTCUSDT")
        let paths = t.calls.map(\.url.path).filter { $0 != "/fapi/v1/time" }
        XCTAssertEqual(paths, ["/fapi/v1/positionSide/dual", "/fapi/v1/marginType", "/fapi/v1/leverage"])
        let m = t.calls.first { $0.url.path == "/fapi/v1/marginType" }!
        XCTAssertEqual(t.query(m)["marginType"], "CROSSED")
        let l = t.calls.first { $0.url.path == "/fapi/v1/leverage" }!
        XCTAssertEqual(t.query(l)["leverage"], "20")
    }

    func testHedgeModeRejected() async throws {
        let (c, _) = try client(["GET /fapi/v1/positionSide/dual": (200, "{\"dualSidePosition\": true}")])
        do { try await c.ensureAccountSettings("BTCUSDT"); XCTFail("should throw") }
        catch let e as ClientError { XCTAssertEqual(e.code, "HEDGE_MODE") }
    }

    func testMarginTypeOtherErrorRejected() async throws {
        let (c, _) = try client([
            "GET /fapi/v1/positionSide/dual": (200, "{\"dualSidePosition\": false}"),
            "POST /fapi/v1/marginType": (400, "{\"code\": -4048, \"msg\": \"Margin type cannot be changed if there exists position.\"}"),
        ])
        do { try await c.ensureAccountSettings("BTCUSDT"); XCTFail("should throw") }
        catch let e as ClientError { XCTAssertEqual(e.code, "-4048") }
    }

    func testLeverageMismatchRejected() async throws {
        let (c, _) = try client([
            "GET /fapi/v1/positionSide/dual": (200, "{\"dualSidePosition\": false}"),
            "POST /fapi/v1/marginType": (200, "{\"code\": 200, \"msg\": \"success\"}"),
            "POST /fapi/v1/leverage": (200, "{\"leverage\": 10}"),
        ])
        do { try await c.ensureAccountSettings("BTCUSDT"); XCTFail("should throw") }
        catch let e as ClientError { XCTAssertEqual(e.code, "LEVERAGE") }
    }

    func testPositionsMatchAlgoStops() {
        let raw: [[String: Any]] = [
            ["symbol": "BTCUSDT", "positionAmt": "0.05", "entryPrice": "100000", "markPrice": "100100", "unRealizedProfit": "5"],
            ["symbol": "ETHUSDT", "positionAmt": "-1", "entryPrice": "3000", "markPrice": "2990", "unRealizedProfit": "10"],
            ["symbol": "SOLUSDT", "positionAmt": "0", "entryPrice": "0", "markPrice": "0"],
        ]
        let algo: [[String: Any]] = [["symbol": "BTCUSDT", "orderType": "STOP_MARKET", "side": "SELL", "reduceOnly": true, "triggerPrice": "99000", "algoId": 9]]
        let ps = BinanceSignedExchange.mapPositions(raw, algoOrders: algo, legacyOrders: [])
        XCTAssertEqual(ps.count, 2)
        XCTAssertTrue(ps[0].protected); XCTAssertEqual(ps[0].stop, 99000)
        XCTAssertFalse(ps[1].protected); XCTAssertEqual(ps[1].side, .short)
    }

    func testErrorsNeverLeakSignedURL() async throws {
        let (c, _) = try client(["GET /fapi/v2/account": (401, "{\"code\": -2015, \"msg\": \"Invalid API-key, IP, or permissions for action.\"}")])
        do { _ = try await c.balance(); XCTFail() }
        catch let e as ClientError {
            XCTAssertEqual(e.code, "-2015"); XCTAssertFalse(e.message.contains("signature")); XCTAssertFalse(e.message.contains("SECRET"))
        }
    }

    func testWatchlistValidation() async throws {
        let t = FakeTransport()
        let market = PublicMarket(http: t)
        let wl = WatchlistStore(url: tmpDir().appendingPathComponent("w.json"))
        t.routes["/fapi/v1/exchangeInfo"] = (200, "{\"symbols\":[{\"symbol\":\"DOGEUSDT\",\"contractType\":\"PERPETUAL\",\"quoteAsset\":\"USDT\",\"status\":\"TRADING\"}]}")
        let ok = await wl.add("dogeusdt", market: market)
        XCTAssertTrue(ok.ok); XCTAssertEqual(ok.symbols, ["BTCUSDT", "DOGEUSDT"])
        t.routes["/fapi/v1/exchangeInfo"] = (200, "{\"symbols\":[{\"symbol\":\"BTCUSDT_261225\",\"contractType\":\"CURRENT_QUARTER\",\"quoteAsset\":\"USDT\",\"status\":\"TRADING\"}]}")
        let bad = await wl.add("BTCUSDT_261225", market: market)
        XCTAssertFalse(bad.ok); XCTAssertEqual(bad.reasonZh, "该交易对不是 U 本位永续合约，无法加入自选")
        t.routes["/fapi/v1/exchangeInfo"] = (400, "{\"code\":-1121,\"msg\":\"Invalid symbol.\"}")
        let missing = await wl.add("NOPEUSDT", market: market)
        XCTAssertFalse(missing.ok)
        XCTAssertEqual(try wl.remove("DOGEUSDT"), ["BTCUSDT"])
    }

    func testParseFilters() {
        let info: [String: Any] = ["symbols": [["symbol": "BTCUSDT", "filters": [
            ["filterType": "PRICE_FILTER", "tickSize": "0.10"],
            ["filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"],
            ["filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.002"],
            ["filterType": "MIN_NOTIONAL", "notional": "100"],
        ]]]]
        let f = ExchangeInfo.parseFilters(info, symbol: "BTCUSDT")!
        XCTAssertEqual(f.tickSize, D("0.1")); XCTAssertEqual(f.minQty, D("0.002")); XCTAssertEqual(f.minNotional, 100)
    }
}
