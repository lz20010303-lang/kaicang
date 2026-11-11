import Foundation

/// 写死的风控规则。没有任何可配置入口，UI 与设置都无法修改。
public enum RiskRules {
    public static let riskFraction: Decimal = Decimal(string: "0.05")!
    public static let leverage: Int = 20
    public static let marginMode: String = "CROSSED"
}

public enum Side: String, Codable, Sendable, CaseIterable {
    case long = "LONG"
    case short = "SHORT"

    public var zh: String { self == .long ? "做多" : "做空" }
    public var entryOrderSide: String { self == .long ? "BUY" : "SELL" }
    public var exitOrderSide: String { self == .long ? "SELL" : "BUY" }

    /// 与 Python `_normalize_side` 一致：nil/空 → nil；无法识别 → .invalid。
    public static func normalize(_ text: String?) -> SideParse {
        guard let raw = text else { return .missing }
        let t = raw.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
        if t.isEmpty { return .missing }
        if t == "LONG" || t == "BUY" { return .side(.long) }
        if t == "SHORT" || t == "SELL" { return .side(.short) }
        return .invalid
    }
}

public enum SideParse: Equatable { case missing, invalid, side(Side) }

public struct SymbolFilters: Equatable, Sendable, Codable {
    public let tickSize: Decimal
    public let stepSize: Decimal
    public let minQty: Decimal
    public let minNotional: Decimal

    public init?(tickSize: Decimal?, stepSize: Decimal?, minQty: Decimal?, minNotional: Decimal?) {
        guard let t = tickSize, let s = stepSize, let q = minQty, let n = minNotional,
              t > 0, s > 0, q > 0, n > 0 else { return nil }
        self.tickSize = t; self.stepSize = s; self.minQty = q; self.minNotional = n
    }
}

public struct Decision: Equatable, Sendable {
    public var status: String          // "PASS" / "REJECT"
    public var reasonCode: String
    public var reasonZh: String
    public var side: Side? = nil
    public var mark: Decimal? = nil
    public var stop: Decimal? = nil
    public var riskUsd: Decimal? = nil
    public var quantity: Decimal? = nil
    public var rawQuantity: Decimal? = nil
    public var notional: Decimal? = nil
    public let riskFraction: Decimal = RiskRules.riskFraction
    public let leverage: Int = RiskRules.leverage
    public let marginMode: String = RiskRules.marginMode

    public var passed: Bool { status == "PASS" }

    static func reject(_ code: String, _ zh: String, side: Side? = nil, mark: Decimal? = nil, stop: Decimal? = nil,
                       riskUsd: Decimal? = nil, quantity: Decimal? = nil, rawQuantity: Decimal? = nil,
                       notional: Decimal? = nil) -> Decision {
        Decision(status: "REJECT", reasonCode: code, reasonZh: zh, side: side, mark: mark, stop: stop,
                 riskUsd: riskUsd, quantity: quantity, rawQuantity: rawQuantity, notional: notional)
    }

    public static func == (a: Decision, b: Decision) -> Bool {
        a.status == b.status && a.reasonCode == b.reasonCode && a.side == b.side && a.mark == b.mark
            && a.stop == b.stop && a.quantity == b.quantity && a.notional == b.notional && a.riskUsd == b.riskUsd
    }
}

public enum RiskEngine {
    /// 止损按 tick 对齐，且不会收紧风险：做多向下取整，做空向上取整。
    public static func alignStop(_ stop: Decimal, tick: Decimal, side: Side) -> Decimal {
        let ratio = stop / tick
        let steps = side == .long ? Dec.truncInt(ratio) : Dec.awayInt(ratio)
        return steps * tick
    }

    static func quantizeDown(_ value: Decimal, step: Decimal) -> Decimal {
        Dec.truncInt(value / step) * step
    }

    /// 开仓判定。没有数量参数：仓位永远由 risk_usd / |mark − stop| 计算并向下取整。
    public static func evaluate(side sideText: String?, stop stopText: String?, mark: Decimal?, balance: Decimal?,
                                filters: SymbolFilters?) -> Decision {
        guard let markD = mark, let balanceD = balance, markD.isFinite, balanceD.isFinite, markD > 0, balanceD >= 0 else {
            return .reject("INVALID_INPUT", "输入数据缺失或无效（余额或标记价格）")
        }
        guard let filters else {
            return .reject("INVALID_INPUT", "输入数据缺失或无效（合约过滤器）", mark: markD)
        }
        let side: Side
        switch Side.normalize(sideText) {
        case .missing: return .reject("INVALID_SIDE", "请选择开仓方向", mark: markD)
        case .invalid: return .reject("INVALID_SIDE", "方向无效，只能是做多或做空", mark: markD)
        case .side(let s): side = s
        }
        let trimmed = stopText?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if trimmed.isEmpty {
            return .reject("MISSING_STOP", "止损价为必填项，不填不允许开单", side: side, mark: markD)
        }
        guard let stopD = Dec.parse(trimmed), stopD > 0 else {
            return .reject("INVALID_STOP", "止损价无效", side: side, mark: markD)
        }
        let aligned = alignStop(stopD, tick: filters.tickSize, side: side)
        if side == .long && aligned >= markD {
            return .reject("STOP_NOT_BELOW_MARK", "做多时止损价必须低于标记价格", side: side, mark: markD, stop: aligned)
        }
        if side == .short && aligned <= markD {
            return .reject("STOP_NOT_ABOVE_MARK", "做空时止损价必须高于标记价格", side: side, mark: markD, stop: aligned)
        }
        let distance = abs(markD - aligned)
        if distance <= 0 || !distance.isFinite {
            return .reject("ZERO_DISTANCE", "止损距离必须大于 0", side: side, mark: markD, stop: aligned)
        }
        let risk = balanceD * RiskRules.riskFraction
        if risk <= 0 || !risk.isFinite {
            return .reject("INVALID_INPUT", "风险金额无效（账户余额需大于 0）", side: side, mark: markD, stop: aligned, riskUsd: risk)
        }
        let raw = risk / distance
        let qty = quantizeDown(raw, step: filters.stepSize)
        if qty < filters.minQty || qty <= 0 {
            return .reject("QTY_BELOW_MIN", "计算数量不足最小下单量，已拒绝（不会向上取整）", side: side, mark: markD,
                           stop: aligned, riskUsd: risk, quantity: qty, rawQuantity: raw)
        }
        let notional = qty * markD
        if notional < filters.minNotional {
            return .reject("NOTIONAL_BELOW_MIN", "名义价值不足最小名义金额，已拒绝（不会向上取整凑手数）", side: side,
                           mark: markD, stop: aligned, riskUsd: risk, quantity: qty, rawQuantity: raw, notional: notional)
        }
        return Decision(status: "PASS", reasonCode: "PASS", reasonZh: "通过", side: side, mark: markD, stop: aligned,
                        riskUsd: risk, quantity: qty, rawQuantity: raw, notional: notional)
    }

    public static let notInWatchlistZh = "该交易对不在自选中，请先加入自选后再开单"

    /// 硬门槛：只有已在自选里的交易对才能开单。
    public static func requireInWatchlist(_ symbol: String?, _ watchlist: [String]?) -> Decision? {
        guard let watchlist else { return .reject("NOT_IN_WATCHLIST", notInWatchlistZh) }
        let allowed = Set(watchlist.map { $0.trimmingCharacters(in: .whitespaces).uppercased() }.filter { !$0.isEmpty })
        let sym = (symbol ?? "").trimmingCharacters(in: .whitespaces).uppercased()
        if sym.isEmpty || !allowed.contains(sym) { return .reject("NOT_IN_WATCHLIST", notInWatchlistZh) }
        return nil
    }
}
