import Foundation

/// 均线 / Supertrend：用币安官方 U 本位 K 线在本地计算（币安没有指标接口）。
/// 只用已收盘的 K 线；数据不足或无效时不编造数值。规则与 Python 版 app/indicators.py 一致。
public enum Interval: String, CaseIterable, Codable, Sendable, Identifiable {
    case m15 = "15m", h1 = "1h", h4 = "4h", d1 = "1d"
    public var id: String { rawValue }
    public var label: String {
        switch self { case .m15: return "15min"; case .h1: return "1h"; case .h4: return "4h"; case .d1: return "1日" }
    }
    /// MACD 背驰用的小一级周期（15m 用 5m）。
    public var lower: String {
        switch self { case .d1: return "4h"; case .h4: return "1h"; case .h1: return "15m"; case .m15: return "5m" }
    }
    public static func label(for raw: String) -> String {
        if let i = Interval(rawValue: raw) { return i.label }
        if raw == "5m" { return "5min" }
        return raw
    }
}

public enum StopSource: String, CaseIterable, Codable, Sendable, Identifiable {
    case ma, supertrend
    public var id: String { rawValue }
    public var label: String { self == .ma ? "均线" : "Supertrend" }
}

public enum IndicatorParams {
    public static let maPeriod = 20
    public static let supertrendAtrPeriod = 10
    public static let supertrendMultiplier: Decimal = 3
    public static let klineLimit = 200
    public static var maLabel: String { "SMA\(maPeriod)" }
    public static var supertrendLabel: String { "ATR\(supertrendAtrPeriod) × \(Dec.str(supertrendMultiplier))" }
}

public struct Candle: Equatable, Sendable, Codable {
    public let openTime: Int64
    public let closeTime: Int64
    public let open: Decimal
    public let high: Decimal
    public let low: Decimal
    public let close: Decimal

    public init(openTime: Int64, closeTime: Int64, open: Decimal, high: Decimal, low: Decimal, close: Decimal) {
        self.openTime = openTime; self.closeTime = closeTime
        self.open = open; self.high = high; self.low = low; self.close = close
    }

    /// 解析币安 K 线数组 [openTime, open, high, low, close, volume, closeTime, ...]。
    public static func parse(_ row: Any) -> Candle? {
        guard let arr = row as? [Any], arr.count >= 7 else { return nil }
        guard let ot = int64(arr[0]), let ct = int64(arr[6]) else { return nil }
        guard let o = Dec.from(arr[1]), let h = Dec.from(arr[2]), let l = Dec.from(arr[3]), let c = Dec.from(arr[4]) else { return nil }
        guard [o, h, l, c].allSatisfy({ $0.isFinite && $0 > 0 }), h >= l else { return nil }
        return Candle(openTime: ot, closeTime: ct, open: o, high: h, low: l, close: c)
    }

    static func int64(_ any: Any) -> Int64? {
        if let i = any as? Int64 { return i }
        if let i = any as? Int { return Int64(i) }
        if let n = any as? NSNumber { return n.int64Value }
        if let s = any as? String { return Int64(s) }
        return nil
    }
}

public struct IndicatorCompute: Sendable {
    public let value: Decimal?
    public let candle: Candle?
    public let reasonZh: String?
    public var ok: Bool { value?.isFinite ?? false }
}

public struct StopFill: Sendable, Equatable {
    public let interval: String
    public let source: StopSource
    public let value: Decimal?
    public let fill: Decimal?
    public let usableForSide: Bool
    public let reason: String
    public let candle: Candle?
    public let mark: Decimal?
    public let paramsLabel: String
}

public enum Indicators {
    public static let shanghai = TimeZone(secondsFromGMT: 8 * 3600)!

    public static func candleLabel(_ interval: String, _ candle: Candle?) -> String? {
        guard let candle else { return nil }
        let date = Date(timeIntervalSince1970: TimeInterval(candle.closeTime) / 1000)
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = shanghai
        f.dateFormat = "yyyy-MM-dd HH:mm"
        return "\(Interval.label(for: interval)) 已收盘 K 线，收盘 \(f.string(from: date))（Asia/Shanghai）"
    }

    /// 只保留已完全收盘的 K 线（close_time >= now 的正在形成的 K 线被丢弃），按开盘时间排序。
    public static func closedCandles(_ rows: [Any]?, nowMs: Int64? = nil) -> [Candle] {
        guard let rows, !rows.isEmpty else { return [] }
        let now = nowMs ?? Int64(Date().timeIntervalSince1970 * 1000)
        return rows.compactMap(Candle.parse).filter { $0.closeTime < now }.sorted { $0.openTime < $1.openTime }
    }

    public static func smaFromCloses(_ closes: [Decimal], period: Int) -> Decimal? {
        guard period > 0, closes.count >= period else { return nil }
        let window = closes.suffix(period)
        guard window.allSatisfy({ $0.isFinite }) else { return nil }
        return window.reduce(Decimal(0), +) / Decimal(period)
    }

    public static func computeSMA(_ candles: [Candle], period: Int) -> IndicatorCompute {
        if period <= 0 { return IndicatorCompute(value: nil, candle: nil, reasonZh: "均线周期无效") }
        if candles.count < period {
            return IndicatorCompute(value: nil, candle: candles.last,
                                    reasonZh: "已收盘 K 线不足，无法计算均线（需要 \(period) 根，当前 \(candles.count) 根）")
        }
        guard let v = smaFromCloses(candles.map(\.close), period: period), v.isFinite else {
            return IndicatorCompute(value: nil, candle: candles.last, reasonZh: "均线值无效，未填写止损")
        }
        return IndicatorCompute(value: v, candle: candles.last, reasonZh: nil)
    }

    static func trueRanges(_ candles: [Candle]) -> [Decimal] {
        var out: [Decimal] = []
        var prevClose: Decimal? = nil
        for c in candles {
            let hl = c.high - c.low
            if let pc = prevClose {
                out.append(max(hl, abs(c.high - pc), abs(c.low - pc)))
            } else {
                out.append(hl)
            }
            prevClose = c.close
        }
        return out
    }

    /// Wilder ATR，与 candles 对齐；第一个完整窗口之前为 nil。
    public static func wilderATR(_ candles: [Candle], period: Int) -> [Decimal?] {
        var atrs = [Decimal?](repeating: nil, count: candles.count)
        guard period > 0, !candles.isEmpty else { return atrs }
        let trs = trueRanges(candles)
        guard trs.count >= period else { return atrs }
        let first = trs[0..<period].reduce(Decimal(0), +) / Decimal(period)
        atrs[period - 1] = first
        var prev = first
        if period < trs.count {
            for i in period..<trs.count {
                prev = (prev * Decimal(period - 1) + trs[i]) / Decimal(period)
                atrs[i] = prev
            }
        }
        return atrs
    }

    /// 标准 Supertrend（HL2 / Wilder ATR），只返回最后一根已收盘 K 线的值。
    public static func computeSupertrend(_ candles: [Candle], period: Int, multiplier: Decimal) -> IndicatorCompute {
        if period <= 0 { return IndicatorCompute(value: nil, candle: nil, reasonZh: "Supertrend 周期无效") }
        if !multiplier.isFinite || multiplier <= 0 { return IndicatorCompute(value: nil, candle: nil, reasonZh: "Supertrend 乘数无效") }
        if candles.count < period {
            return IndicatorCompute(value: nil, candle: candles.last,
                                    reasonZh: "已收盘 K 线不足，无法计算 Supertrend（需要至少 \(period) 根，当前 \(candles.count) 根）")
        }
        let atrs = wilderATR(candles, period: period)
        var prevUp: Decimal? = nil
        var prevDown: Decimal? = nil
        var prevTrend = 1
        var last: Decimal? = nil
        var lastCandle: Candle? = nil
        for (i, c) in candles.enumerated() {
            guard let atr = atrs[i], atr.isFinite else { continue }
            let hl2 = (c.high + c.low) / 2
            let up = hl2 - multiplier * atr
            let down = hl2 + multiplier * atr
            let prevClose: Decimal? = i > 0 ? candles[i - 1].close : nil
            var trendUp: Decimal, trendDown: Decimal, trend: Int
            if let pu = prevUp, let pd = prevDown, let pc = prevClose {
                trendUp = pc > pu ? (up > pu ? up : pu) : up
                trendDown = pc < pd ? (down < pd ? down : pd) : down
                if c.close > pd { trend = 1 } else if c.close < pu { trend = -1 } else { trend = prevTrend }
            } else {
                trendUp = up; trendDown = down; trend = 1
            }
            last = trend == 1 ? trendUp : trendDown
            lastCandle = c
            prevUp = trendUp; prevDown = trendDown; prevTrend = trend
        }
        guard let v = last, v.isFinite, let lc = lastCandle else {
            return IndicatorCompute(value: nil, candle: candles.last, reasonZh: "Supertrend 值无效，未填写止损")
        }
        return IndicatorCompute(value: v, candle: lc, reasonZh: nil)
    }

    public static func compute(_ candles: [Candle], source: StopSource) -> IndicatorCompute {
        switch source {
        case .ma: return computeSMA(candles, period: IndicatorParams.maPeriod)
        case .supertrend: return computeSupertrend(candles, period: IndicatorParams.supertrendAtrPeriod,
                                                   multiplier: IndicatorParams.supertrendMultiplier)
        }
    }

    public static func paramsLabel(_ source: StopSource) -> String {
        source == .ma ? IndicatorParams.maLabel : IndicatorParams.supertrendLabel
    }

    static func name(_ s: StopSource) -> String { s == .ma ? "均线" : "Supertrend" }
    static func periodName(_ s: StopSource) -> String { s == .ma ? "当前周期均线" : "当前周期 Supertrend " }

    /// 指标值能否自动填入止损。永不猜测：做多只填价格下方的线，做空只填上方的线；
    /// 未选方向时只填价格下方的线。
    public static func suggestStop(interval: String, source: StopSource, value: Decimal?, mark: Decimal?, side: Side?,
                                   tickSize: Decimal?, candle: Candle? = nil, paramsLabel: String = "",
                                   error: String? = nil) -> StopFill {
        let markValue = mark
        func result(raw: Decimal? = nil, fill: Decimal? = nil, usable: Bool = false, reason: String) -> StopFill {
            StopFill(interval: interval, source: source, value: raw, fill: fill, usableForSide: usable, reason: reason,
                     candle: candle, mark: markValue, paramsLabel: paramsLabel)
        }
        if let error, !error.isEmpty { return result(reason: error) }
        guard let value, value.isFinite else { return result(reason: "指标值无效，未填写止损") }
        guard let mark, mark.isFinite, mark > 0 else { return result(raw: value, reason: "标记价格无效，未填写止损") }

        let label = paramsLabel.isEmpty ? name(source) : paramsLabel
        let intervalZh = Interval.label(for: interval)
        let suffix = candleLabel(interval, candle).map { "（\($0)）" } ?? ""
        func aligned(_ raw: Decimal, _ s: Side) -> Decimal {
            if let t = tickSize, t > 0 { return RiskEngine.alignStop(raw, tick: t, side: s) }
            return raw
        }
        func filledReason(_ f: Decimal) -> String { "\(intervalZh) \(label)：\(Dec.str(f))，已填入止损\(suffix)" }

        guard let side else {
            if value < mark {
                let f = aligned(value, .long)
                if f >= mark { return result(raw: value, reason: "对齐后的指标值未低于标记价格，未填写止损") }
                return result(raw: value, fill: f, usable: true, reason: filledReason(f))
            }
            if value > mark {
                return result(raw: value, reason: "已算出\(intervalZh)\(name(source))在价格上方；未选择方向时只自动填入价格下方的线。若要做空，请先选择做空后再试。")
            }
            return result(raw: value, reason: "\(periodName(source))未低于标记价格，未选择方向时不自动填写止损")
        }
        switch side {
        case .long:
            if value < mark {
                let f = aligned(value, .long)
                if f >= mark { return result(raw: value, reason: "对齐后的指标值未低于标记价格，不能作为多头止损") }
                return result(raw: value, fill: f, usable: true, reason: filledReason(f))
            }
            if value > mark { return result(raw: value, reason: "\(periodName(source))在价格上方，不能作为多头止损") }
            return result(raw: value, reason: "\(periodName(source))未低于标记价格，不能作为多头止损")
        case .short:
            if value > mark {
                let f = aligned(value, .short)
                if f <= mark { return result(raw: value, reason: "对齐后的指标值未高于标记价格，不能作为空头止损") }
                return result(raw: value, fill: f, usable: true, reason: filledReason(f))
            }
            if value < mark { return result(raw: value, reason: "\(periodName(source))在价格下方，不能作为空头止损") }
            return result(raw: value, reason: "\(periodName(source))未高于标记价格，不能作为空头止损")
        }
    }
}
