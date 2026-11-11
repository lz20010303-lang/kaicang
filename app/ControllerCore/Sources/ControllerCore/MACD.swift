import Foundation

/// MACD 金叉/死叉与小级别背驰（仅供参考，不进入任何开单风控）。
/// 规则与 Python 版 app/macd.py 一致。
public enum MACDParams {
    public static let fast = 12, slow = 26, signal = 9, lookback = 100
    public static var label: String { "MACD(\(fast),\(slow),\(signal))" }
    /// 与 Python `_macd_kline_limit` 一致：max(200, lookback + slow + signal + 20)。
    public static var klineLimit: Int { max(IndicatorParams.klineLimit, lookback + slow + signal + 20, 200) }
}

public struct MacdPoint: Equatable, Sendable { public let dif, dea, hist: Decimal }

public enum Bias: String, Sendable { case long = "LONG", short = "SHORT" }

public struct CrossResult: Equatable, Sendable {
    public var ok: Bool
    public var type: String?          // "golden" / "death"
    public var typeZh: String
    public var bias: Bias?
    public var barsAgo: Int?
    public var dif: String?, dea: String?, histogram: String?
    public var reasonZh: String
    public var labelZh: String = ""
}

public struct DivergenceResult: Equatable, Sendable {
    public var ok: Bool
    public var type: String?          // "bullish"(底背驰) / "bearish"(顶背驰)
    public var typeZh: String
    public var bias: Bias?
    public var reasonZh: String
    public var lastPriceHigh: String? = nil, lastPriceLow: String? = nil
    public var prevPriceHigh: String? = nil, prevPriceLow: String? = nil
    public var lastMaxDif: String? = nil, lastMinDif: String? = nil
    public var prevMaxDif: String? = nil, prevMinDif: String? = nil
    public var interval: String = ""
    public var labelZh: String = ""
}

public struct MacdAnalysis: Equatable, Sendable {
    public var ok: Bool
    public var interval: String
    public var lowerInterval: String
    public var reasonZh: String
    public var cross: CrossResult
    public var divergence: DivergenceResult
    public let note = "仅供参考，不影响开单"
}

public enum MACD {
    static let quant = 8

    public static func fmt(_ v: Decimal) -> String { Dec.str(Dec.roundHalfEven(v, scale: quant)) }

    /// EMA，以前 period 个值的 SMA 作种子（标准 MACD 算法）。
    public static func emaSeries(_ values: [Decimal], period: Int) -> [Decimal?] {
        var out = [Decimal?](repeating: nil, count: values.count)
        guard period > 0, values.count >= period else { return out }
        let seed = values[0..<period].reduce(Decimal(0), +) / Decimal(period)
        out[period - 1] = seed
        let k = Decimal(2) / Decimal(period + 1)
        let oneMinus = Decimal(1) - k
        var prev = seed
        if period < values.count {
            for i in period..<values.count {
                prev = values[i] * k + prev * oneMinus
                out[i] = prev
            }
        }
        return out
    }

    /// DIF = EMA(fast) − EMA(slow)；DEA = EMA(DIF, signal)；柱 = DIF − DEA。
    public static func points(closes: [Decimal], fast: Int = MACDParams.fast, slow: Int = MACDParams.slow,
                              signal: Int = MACDParams.signal) -> [MacdPoint?] {
        let n = closes.count
        var out = [MacdPoint?](repeating: nil, count: n)
        guard fast > 0, slow > 0, signal > 0, fast < slow, n >= slow + signal - 1 else { return out }
        let ef = emaSeries(closes, period: fast), es = emaSeries(closes, period: slow)
        var dif = [Decimal?](repeating: nil, count: n)
        for i in 0..<n { if let a = ef[i], let b = es[i] { dif[i] = a - b } }
        guard let first = dif.firstIndex(where: { $0 != nil }), n - first >= signal else { return out }
        let tail = emaSeries(dif[first..<n].map { $0! }, period: signal)
        for (offset, dea) in tail.enumerated() {
            let i = first + offset
            guard let dea, let d = dif[i] else { continue }
            out[i] = MacdPoint(dif: d, dea: dea, hist: d - dea)
        }
        return out
    }

    static func cmpZero(_ v: Decimal) -> String { v > 0 ? "above" : (v < 0 ? "below" : "equal") }
    static func cmpZeroZh(_ which: String, _ axis: String) -> String {
        let token = axis == "零轴" ? axis : " \(axis) "
        switch which {
        case "above": return "在\(token)上方"
        case "below": return "在\(token)下方"
        default: return "与\(token)相等"
        }
    }

    /// 最近一次 DIF/DEA 交叉。金叉：上一根 DIF<=DEA 且当前 DIF>DEA；死叉相反。bars_ago=0 表示最后一根已收盘 K 线就是交叉。
    public static func detectCross(_ pts: [MacdPoint?]) -> CrossResult {
        var lastIdx: Int? = nil, lastType: String? = nil, lastValid: Int? = nil, prevValid: Int? = nil
        for (i, p) in pts.enumerated() {
            guard let p else { continue }
            if let pv = prevValid, let prev = pts[pv] {
                if prev.dif <= prev.dea && p.dif > p.dea { lastIdx = i; lastType = "golden" }
                else if prev.dif >= prev.dea && p.dif < p.dea { lastIdx = i; lastType = "death" }
            }
            prevValid = i; lastValid = i
        }
        guard let lv = lastValid, let last = pts[lv] else {
            return CrossResult(ok: false, type: nil, typeZh: "无法判断", bias: nil, barsAgo: nil, dif: nil, dea: nil,
                               histogram: nil, reasonZh: "已收盘 K 线不足，无法计算 MACD")
        }
        let vsDea = cmpZero(last.dif - last.dea), vsZero = cmpZero(last.dif)
        let tail = "当前 DIF \(cmpZeroZh(vsDea, "DEA"))，\(cmpZeroZh(vsZero, "零轴"))"
        guard let t = lastType, let li = lastIdx else {
            return CrossResult(ok: true, type: nil, typeZh: "无交叉", bias: nil, barsAgo: nil, dif: fmt(last.dif),
                               dea: fmt(last.dea), histogram: fmt(last.hist),
                               reasonZh: "可见已收盘 K 线内尚未出现 DIF 与 DEA 的交叉；" + tail)
        }
        let ago = lv - li
        let zh = t == "golden" ? "金叉" : "死叉"
        let when = ago == 0 ? "刚收盘" : "\(ago)根前"
        return CrossResult(ok: true, type: t, typeZh: zh, bias: t == "golden" ? .long : .short, barsAgo: ago,
                           dif: fmt(last.dif), dea: fmt(last.dea), histogram: fmt(last.hist),
                           reasonZh: "最近一次交叉为\(zh)（\(when)）；" + tail)
    }

    struct Segment { var sign: Int; var start: Int; var end: Int; var high: Decimal; var low: Decimal; var maxDif: Decimal; var minDif: Decimal }

    static func segments(_ candles: [Candle], _ pts: [MacdPoint?]) -> [Segment] {
        var segs: [Segment] = []
        var open = false
        for i in 0..<min(candles.count, pts.count) {
            guard let p = pts[i] else { open = false; continue }
            let c = candles[i]
            let sign = p.hist > 0 ? 1 : (p.hist < 0 ? -1 : 0)
            if sign == 0 { open = false; continue }
            if !open || segs[segs.count - 1].sign != sign {
                segs.append(Segment(sign: sign, start: i, end: i, high: c.high, low: c.low, maxDif: p.dif, minDif: p.dif))
                open = true
            } else {
                var s = segs[segs.count - 1]
                s.end = i
                if c.high > s.high { s.high = c.high }
                if c.low < s.low { s.low = c.low }
                if p.dif > s.maxDif { s.maxDif = p.dif }
                if p.dif < s.minDif { s.minDif = p.dif }
                segs[segs.count - 1] = s
            }
        }
        return segs
    }

    /// 按柱符号分段；只比较最后一段与窗口内前一段同色柱，用该段 DIF 极值（不用柱面积），严格不等式。
    public static func detectDivergence(_ candlesIn: [Candle], _ ptsIn: [MacdPoint?], lookback: Int = MACDParams.lookback) -> DivergenceResult {
        var r = DivergenceResult(ok: true, type: nil, typeZh: "无背驰", bias: nil, reasonZh: "")
        if lookback <= 0 { r.ok = false; r.typeZh = "无法判断"; r.reasonZh = "背驰回看窗口无效"; return r }
        let n = min(candlesIn.count, ptsIn.count)
        let candles = Array(candlesIn.prefix(n)), pts = Array(ptsIn.prefix(n))
        let valid = pts.indices.filter { pts[$0] != nil }
        if valid.isEmpty { r.ok = false; r.typeZh = "无法判断"; r.reasonZh = "已收盘 K 线不足，无法计算小级别 MACD"; return r }
        let window = Array(valid.suffix(lookback))
        let start = window.first!, end = window.last!
        let segs = segments(Array(candles[start...end]), Array(pts[start...end]))
        guard let last = segs.last else { r.reasonZh = "小级别窗口内没有可用的 MACD 柱分段，无背驰"; return r }
        r.lastPriceHigh = Dec.str(last.high); r.lastPriceLow = Dec.str(last.low)
        r.lastMaxDif = fmt(last.maxDif); r.lastMinDif = fmt(last.minDif)
        guard let prev = segs.dropLast().last(where: { $0.sign == last.sign }) else {
            r.reasonZh = "小级别窗口内不足两段同向\(last.sign > 0 ? "红柱" : "绿柱")，无背驰"; return r
        }
        r.prevPriceHigh = Dec.str(prev.high); r.prevPriceLow = Dec.str(prev.low)
        r.prevMaxDif = fmt(prev.maxDif); r.prevMinDif = fmt(prev.minDif)
        if last.sign > 0 {
            if last.high > prev.high && last.maxDif < prev.maxDif {
                r.type = "bearish"; r.typeZh = "顶背驰"; r.bias = .short
                r.reasonZh = "最近一段红柱的价格高点高于前一段红柱，但该段 DIF 最大值低于前一段，判定为顶背驰"
            } else {
                r.reasonZh = "最近一段红柱与前一段红柱不构成顶背驰（需更高高点且 DIF 最大值更低）"
            }
            return r
        }
        if last.low < prev.low && last.minDif > prev.minDif {
            r.type = "bullish"; r.typeZh = "底背驰"; r.bias = .long
            r.reasonZh = "最近一段绿柱的价格低点低于前一段绿柱，但该段 DIF 最小值高于前一段，判定为底背驰"
        } else {
            r.reasonZh = "最近一段绿柱与前一段绿柱不构成底背驰（需更低低点且 DIF 最小值更高）"
        }
        return r
    }

    static func crossLabel(_ interval: String, _ c: CrossResult, failed: Bool) -> String {
        let prefix = "\(interval) MACD："
        if failed { return prefix + "获取失败" }
        guard c.type != nil, let ago = c.barsAgo else { return prefix + c.typeZh }
        return prefix + "\(c.typeZh)（\(ago == 0 ? "刚收盘" : "\(ago)根前")）"
    }

    static func divLabel(_ lower: String, _ d: DivergenceResult, failed: Bool) -> String {
        "\(lower) 小级别：" + (failed ? "获取失败" : d.typeZh)
    }

    public static func analyze(intervalCandles: [Candle], lowerCandles: [Candle], interval: String, lower: String) -> MacdAnalysis {
        var cross = detectCross(points(closes: intervalCandles.map(\.close)))
        let crossFailed = !cross.ok
        cross.labelZh = crossLabel(interval, cross, failed: crossFailed)
        var div = detectDivergence(lowerCandles, points(closes: lowerCandles.map(\.close)))
        let divFailed = !div.ok
        div.interval = lower
        div.labelZh = divLabel(lower, div, failed: divFailed)
        var reason = "仅供参考，不影响开单"
        if crossFailed && divFailed { reason = cross.reasonZh }
        else if crossFailed { reason = cross.reasonZh }
        else if divFailed { reason = div.reasonZh }
        return MacdAnalysis(ok: !crossFailed && !divFailed, interval: interval, lowerInterval: lower, reasonZh: reason,
                            cross: cross, divergence: div)
    }

    /// 网络失败时的占位结果：全部显示「获取失败」，灰色。
    public static func failure(interval: String, lower: String, reason: String) -> MacdAnalysis {
        var c = CrossResult(ok: false, type: nil, typeZh: "获取失败", bias: nil, barsAgo: nil, dif: nil, dea: nil, histogram: nil, reasonZh: reason)
        c.labelZh = crossLabel(interval, c, failed: true)
        var d = DivergenceResult(ok: false, type: nil, typeZh: "获取失败", bias: nil, reasonZh: reason)
        d.interval = lower
        d.labelZh = divLabel(lower, d, failed: true)
        return MacdAnalysis(ok: false, interval: interval, lowerInterval: lower, reasonZh: reason, cross: c, divergence: d)
    }

    /// 参考标注颜色：与开单方向一致 → 绿；相反 → 红；未选方向/无信号/失败 → 灰。
    public enum Tone: String, Sendable { case green, red, gray }
    public static func tone(bias: Bias?, side: Side?) -> Tone {
        guard let bias, let side else { return .gray }
        return bias.rawValue == side.rawValue ? .green : .red
    }
}
