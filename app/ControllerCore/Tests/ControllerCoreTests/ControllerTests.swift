import XCTest
@testable import ControllerCore

final class ControllerTests: XCTestCase {
    func makeMock(mark: Decimal = 100000) -> (OpenController, MockExchange, URL) {
        let dir = tmpDir()
        let fake = FakeTransport()   // 没有路由：任何意外的网络访问都会失败
        let mock = MockExchange(ledgerURL: dir.appendingPathComponent("ledger.json"), market: PublicMarket(http: fake))
        mock.setMark("BTCUSDT", mark)
        mock.setFilters("BTCUSDT", btcFilters)
        let wl = WatchlistStore(url: dir.appendingPathComponent("watchlist.json"))
        let ctl = OpenController(exchange: mock, watchlist: wl, oplog: OpLog(url: dir.appendingPathComponent("ops.jsonl")), liveArmed: false)
        return (ctl, mock, dir)
    }

    func testMockOpenRecordsProtectedPosition() async throws {
        let (ctl, mock, _) = makeMock()
        let r = await ctl.open(symbol: "BTCUSDT", side: .long, stop: "99000")
        XCTAssertTrue(r.opened, r.reasonZh)
        XCTAssertEqual(r.decision?.quantity, D("0.05"))
        let ps = try await mock.positions()
        XCTAssertEqual(ps.count, 1); XCTAssertTrue(ps[0].protected); XCTAssertEqual(ps[0].stop, 99000)
        let events = ctl.oplog.read().map(\.event)
        XCTAssertEqual(events, ["stop_placed", "open"])
    }

    func testOnlyWatchlistSymbols() async {
        let (ctl, mock, _) = makeMock()
        mock.setMark("DOGEUSDT", D("0.2")); mock.setFilters("DOGEUSDT", btcFilters)
        let r = await ctl.open(symbol: "DOGEUSDT", side: .long, stop: "0.19")
        XCTAssertFalse(r.opened); XCTAssertEqual(r.reasonCode, "NOT_IN_WATCHLIST")
    }

    func testMissingStopNeverSends() async {
        let stub = StubExchange()
        let dir = tmpDir()
        let wl = WatchlistStore(url: dir.appendingPathComponent("w.json")); try? wl.overwrite(["BTCUSDT"])
        let ctl = OpenController(exchange: stub, watchlist: wl, oplog: OpLog(url: dir.appendingPathComponent("o")), liveArmed: false)
        let r = await ctl.open(symbol: "BTCUSDT", side: .long, stop: "")
        XCTAssertEqual(r.reasonCode, "MISSING_STOP"); XCTAssertTrue(stub.log.isEmpty)
    }

    func testFreshMarkRecheckAtSend() async {
        // 预览时 mark=100，发送时 mark 已跌到 98.5，止损 99 变成在价格上方 → 拒绝，不下单
        let stub = StubExchange(); stub.marks = [D("98.5")]
        let dir = tmpDir()
        let wl = WatchlistStore(url: dir.appendingPathComponent("w.json")); try? wl.overwrite(["BTCUSDT"])
        let ctl = OpenController(exchange: stub, watchlist: wl, oplog: OpLog(url: dir.appendingPathComponent("o")), liveArmed: false)
        let r = await ctl.open(symbol: "BTCUSDT", side: .long, stop: "99")
        XCTAssertEqual(r.reasonCode, "STOP_NOT_BELOW_MARK"); XCTAssertTrue(stub.log.isEmpty)
    }

    func ctl(_ stub: StubExchange, armed: Bool = false) -> OpenController {
        let dir = tmpDir()
        let wl = WatchlistStore(url: dir.appendingPathComponent("w.json")); try? wl.overwrite(["BTCUSDT"])
        return OpenController(exchange: stub, watchlist: wl, oplog: OpLog(url: dir.appendingPathComponent("o")), liveArmed: armed)
    }

    func testSettingsBeforeEntryAndOrderSequence() async {
        let stub = StubExchange()
        let r = await ctl(stub).open(symbol: "BTCUSDT", side: .long, stop: "99")
        XCTAssertTrue(r.opened)
        XCTAssertEqual(stub.log, ["settings", "open LONG 50", "stop 99 50"])
    }

    func testSettingsFailureBlocksEntry() async {
        let stub = StubExchange(); stub.failSettings = true
        let r = await ctl(stub).open(symbol: "BTCUSDT", side: .long, stop: "99")
        XCTAssertFalse(r.opened); XCTAssertEqual(stub.log, ["settings"])
    }

    func testStopFailureFlattens() async {
        let stub = StubExchange(); stub.failStop = true
        let r = await ctl(stub).open(symbol: "BTCUSDT", side: .short, stop: "101")
        XCTAssertEqual(r.reasonCode, "STOP_FAILED_FLATTENED"); XCTAssertFalse(r.unprotected)
        XCTAssertEqual(stub.log.last, "flatten 50")
    }

    func testStopAndFlattenFailureIsUnprotected() async {
        let stub = StubExchange(); stub.failStop = true; stub.failFlatten = true
        let r = await ctl(stub).open(symbol: "BTCUSDT", side: .long, stop: "99")
        XCTAssertEqual(r.reasonCode, "STOP_FAILED_UNPROTECTED"); XCTAssertTrue(r.unprotected)
    }

    func testLiveRequiresArming() async {
        let stub = StubExchange(mode: .live)
        let r = await ctl(stub, armed: false).open(symbol: "BTCUSDT", side: .long, stop: "99")
        XCTAssertEqual(r.reasonCode, "LIVE_NOT_ARMED"); XCTAssertTrue(stub.log.isEmpty)
        let ok = await ctl(StubExchange(mode: .live), armed: true).open(symbol: "BTCUSDT", side: .long, stop: "99")
        XCTAssertTrue(ok.opened)
    }

    func testMockStopTrigger() async throws {
        let (ctl, mock, _) = makeMock()
        _ = await ctl.open(symbol: "BTCUSDT", side: .long, stop: "99000")
        mock.setMark("BTCUSDT", 98900)
        let hits = await ctl.sweepMockStops()
        XCTAssertEqual(hits.count, 1)
        XCTAssertEqual(hits[0].realizedPnl, D("-55"))           // (98900-100000)*0.05
        XCTAssertEqual(hits[0].walletBalance, 945)
        let ps = try await mock.positions(); XCTAssertTrue(ps.isEmpty)
        XCTAssertEqual(ctl.oplog.read().first?.event, "stop_triggered")
    }

    func testMockMarginCheck() async {
        // 余额 1000，20x → 最多 20000 名义；止损距离很小时仓位过大会被模拟交易所拒绝
        let (ctl, _, _) = makeMock()
        let r = await ctl.open(symbol: "BTCUSDT", side: .long, stop: "99800")   // 50/200=0.25 BTC = 25000 名义
        XCTAssertFalse(r.opened); XCTAssertEqual(r.reasonCode, "INSUFFICIENT_MARGIN")
    }
}
