import XCTest
@testable import ControllerCore

final class IndicatorTests: XCTestCase {
    func row(_ i: Int, _ o: String, _ h: String, _ l: String, _ c: String) -> [Any] {
        [Int64(i) * 900_000, o, h, l, c, "1", Int64(i) * 900_000 + 899_999]
    }

    func testClosedCandlesDropFormingAndInvalid() {
        let rows: [Any] = [row(2, "1", "2", "0.5", "1.5"), row(1, "1", "2", "0.5", "1.2"), row(3, "1", "0.5", "2", "1"), ["bad"]]
        let c = Indicators.closedCandles(rows, nowMs: 2 * 900_000 + 10)   // 第 2 根还没收盘
        XCTAssertEqual(c.count, 1)
        XCTAssertEqual(c[0].close, D("1.2"))
    }

    func testSMA() {
        let candles = (0..<25).map { Candle(openTime: Int64($0), closeTime: Int64($0), open: 1, high: 2, low: 1, close: Decimal($0 + 1)) }
        XCTAssertEqual(Indicators.computeSMA(candles, period: 20).value, D("15.5"))   // 6..25 平均
        XCTAssertNil(Indicators.computeSMA(Array(candles.prefix(5)), period: 20).value)
    }

    func testSuggestStopSides() {
        let long = Indicators.suggestStop(interval: "1h", source: .ma, value: D("95.123"), mark: 100, side: .long, tickSize: D("0.01"))
        XCTAssertEqual(long.fill, D("95.12")); XCTAssertTrue(long.usableForSide)
        let wrong = Indicators.suggestStop(interval: "1h", source: .ma, value: D("105"), mark: 100, side: .long, tickSize: D("0.01"))
        XCTAssertNil(wrong.fill); XCTAssertEqual(wrong.reason, "当前周期均线在价格上方，不能作为多头止损")
        let short = Indicators.suggestStop(interval: "1h", source: .supertrend, value: D("105.001"), mark: 100, side: .short, tickSize: D("0.01"))
        XCTAssertEqual(short.fill, D("105.01"))
        let none = Indicators.suggestStop(interval: "1h", source: .ma, value: D("105"), mark: 100, side: nil, tickSize: nil)
        XCTAssertNil(none.fill)
        let err = Indicators.suggestStop(interval: "1h", source: .ma, value: nil, mark: 100, side: .long, tickSize: nil, error: "网络失败")
        XCTAssertNil(err.fill); XCTAssertEqual(err.reason, "网络失败")
    }

    func testMacdToneAndLowerInterval() {
        XCTAssertEqual(MACD.tone(bias: .long, side: .long), .green)
        XCTAssertEqual(MACD.tone(bias: .long, side: .short), .red)
        XCTAssertEqual(MACD.tone(bias: nil, side: .short), .gray)
        XCTAssertEqual(MACD.tone(bias: .short, side: nil), .gray)
        XCTAssertEqual(Interval.m15.lower, "5m"); XCTAssertEqual(Interval.d1.lower, "4h")
    }

    func testMacdNeedsEnoughBars() {
        let candles = (0..<20).map { Candle(openTime: Int64($0), closeTime: Int64($0), open: 1, high: 2, low: 1, close: Decimal($0 + 1)) }
        let a = MACD.analyze(intervalCandles: candles, lowerCandles: candles, interval: "15m", lower: "5m")
        XCTAssertFalse(a.ok)
        XCTAssertEqual(a.cross.labelZh, "15m MACD：获取失败")
    }
}
