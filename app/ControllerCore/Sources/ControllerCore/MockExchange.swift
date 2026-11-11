import Foundation

/// 模拟账本：余额、持仓、订单存在 App 文档目录的 JSON 里；mark 与过滤器取币安公开数据。
/// 与 Python MockClient 不同的一点：公开接口取不到 mark / 过滤器时直接报错，不使用回退价格。
public final class MockExchange: Exchange, @unchecked Sendable {
    public let mode: TradingMode = .mock
    public static let defaultBalance: Decimal = 1000
    public static let maxOpenPositions = 20
    static let maxClosedHistory = 200, maxOrderHistory = 600

    public struct Position: Codable, Equatable {
        public var id: String, symbol: String, side: Side
        public var quantity: String, entry: String, stop: String, notional: String, riskUsd: String
        public var entryOrderId: String, stopOrderId: String?
        public var status: String   // open / unprotected / flattened / stopped
        public var time: Int64
        public var closePrice: String? = nil, realizedPnl: String? = nil, closeTime: Int64? = nil, closeReason: String? = nil
    }
    public struct Order: Codable, Equatable {
        public var orderId: String, symbol: String, side: String, type: String, status: String
        public var avgPrice: String? = nil, executedQty: String? = nil, stopPrice: String? = nil, origQty: String? = nil
        public var reduceOnly: Bool? = nil
    }
    public struct Ledger: Codable, Equatable {
        public var walletBalance: String
        public var positions: [Position]
        public var orders: [Order]
    }
    public struct StopHit: Equatable, Sendable {
        public let symbol: String, side: Side, quantity: Decimal, entry: Decimal, stop: Decimal, fillPrice: Decimal, realizedPnl: Decimal, walletBalance: Decimal
    }

    public let ledgerURL: URL
    let market: PublicMarket
    private var markOverride: [String: Decimal] = [:]
    private var filterOverride: [String: SymbolFilters] = [:]
    private let lock = NSRecursiveLock()

    public init(ledgerURL: URL, market: PublicMarket = PublicMarket()) {
        self.ledgerURL = ledgerURL; self.market = market
    }

    // 测试用钩子
    public func setMark(_ symbol: String, _ mark: Decimal) { lock.lock(); markOverride[symbol.uppercased()] = mark; lock.unlock() }
    public func setFilters(_ symbol: String, _ f: SymbolFilters) { lock.lock(); filterOverride[symbol.uppercased()] = f; lock.unlock() }

    public func load() -> Ledger {
        lock.lock(); defer { lock.unlock() }
        if let data = try? Data(contentsOf: ledgerURL), let l = try? JSONDecoder().decode(Ledger.self, from: data) { return l }
        return Ledger(walletBalance: Dec.str(MockExchange.defaultBalance), positions: [], orders: [])
    }

    func save(_ ledger: Ledger) throws {
        lock.lock(); defer { lock.unlock() }
        var l = ledger
        let live = l.positions.filter { ["open", "unprotected"].contains($0.status) }
        let closed = l.positions.filter { !["open", "unprotected"].contains($0.status) }
        if closed.count > MockExchange.maxClosedHistory { l.positions = Array(closed.suffix(MockExchange.maxClosedHistory)) + live }
        if l.orders.count > MockExchange.maxOrderHistory { l.orders = Array(l.orders.suffix(MockExchange.maxOrderHistory)) }
        try FileManager.default.createDirectory(at: ledgerURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        let enc = JSONEncoder(); enc.outputFormatting = [.prettyPrinted, .sortedKeys]
        try enc.encode(l).write(to: ledgerURL, options: .atomic)
    }

    /// 重置模拟账户（清空持仓，余额回到 1000 USDT）。
    public func reset() throws { try save(Ledger(walletBalance: Dec.str(MockExchange.defaultBalance), positions: [], orders: [])) }

    static func newId(_ prefix: String) -> String { "\(prefix)-\(UUID().uuidString.lowercased().replacingOccurrences(of: "-", with: "").prefix(12))" }

    public func markPrice(_ symbol: String) async throws -> Decimal {
        let key = symbol.uppercased()
        if let m = lockedGet({ markOverride[key] }) { return m }
        return try await market.markPrice(key)
    }

    func lockedGet<T>(_ body: () -> T) -> T { lock.lock(); defer { lock.unlock() }; return body() }

    public func filters(_ symbol: String) async throws -> SymbolFilters {
        if let f = lockedGet({ filterOverride[symbol.uppercased()] }) { return f }
        return try await market.filters(symbol)
    }

    func marks(for ledger: Ledger, extra: [String: Decimal] = [:]) async throws -> [String: Decimal] {
        var out = extra
        for p in ledger.positions where ["open", "unprotected"].contains(p.status) && out[p.symbol] == nil {
            out[p.symbol] = try await markPrice(p.symbol)
        }
        return out
    }

    static func equity(_ l: Ledger, marks: [String: Decimal]) -> Decimal {
        var upnl: Decimal = 0
        for p in l.positions where ["open", "unprotected"].contains(p.status) {
            let q = Dec.parse(p.quantity) ?? 0, e = Dec.parse(p.entry) ?? 0, m = marks[p.symbol] ?? e
            upnl += p.side == .long ? (m - e) * q : (e - m) * q
        }
        return (Dec.parse(l.walletBalance) ?? 0) + upnl
    }

    public func balance() async throws -> (Decimal, String) {
        let l = load()
        return (MockExchange.equity(l, marks: try await marks(for: l)), "模拟权益（wallet + 未实现盈亏）")
    }

    public func ensureAccountSettings(_ symbol: String) async throws {}

    public func marketOpen(_ symbol: String, side: Side, quantity: Decimal) async throws -> OrderResult {
        let mark = try await markPrice(symbol)
        var l = load()
        let notional = quantity * mark
        let equity = MockExchange.equity(l, marks: try await marks(for: l, extra: [symbol: mark]))
        let used = l.positions.filter { ["open", "unprotected"].contains($0.status) }
            .reduce(Decimal(0)) { $0 + (Dec.parse($1.notional) ?? 0) / Decimal(RiskRules.leverage) }
        let margin = notional / Decimal(RiskRules.leverage)
        if margin > equity - used {
            throw ClientError("模拟交易所拒绝：保证金不足（需要 \(Dec.str(margin)) USDT，可用 \(Dec.str(equity - used)) USDT）", code: "INSUFFICIENT_MARGIN")
        }
        if l.positions.filter({ ["open", "unprotected"].contains($0.status) }).count >= MockExchange.maxOpenPositions {
            throw ClientError("模拟账户最多同时持有 \(MockExchange.maxOpenPositions) 个仓位", code: "MOCK_POSITION_LIMIT")
        }
        let o = Order(orderId: MockExchange.newId("mock-entry"), symbol: symbol, side: side.entryOrderSide, type: "MARKET",
                      status: "FILLED", avgPrice: Dec.str(mark), executedQty: Dec.str(quantity))
        l.orders.append(o)
        try save(l)
        return OrderResult(orderId: o.orderId, avgPrice: mark, raw: ["orderId": o.orderId, "avgPrice": Dec.str(mark)])
    }

    public func placeStop(_ symbol: String, side: Side, stop: Decimal, quantity: Decimal) async throws -> OrderResult {
        var l = load()
        let o = Order(orderId: MockExchange.newId("mock-stop"), symbol: symbol, side: side.exitOrderSide, type: "STOP_MARKET",
                      status: "NEW", stopPrice: Dec.str(stop), origQty: Dec.str(quantity), reduceOnly: true)
        l.orders.append(o)
        try save(l)
        return OrderResult(orderId: o.orderId, avgPrice: nil, raw: ["orderId": o.orderId])
    }

    @discardableResult
    public func recordPosition(symbol: String, side: Side, quantity: Decimal, entry: Decimal, stop: Decimal, notional: Decimal,
                               riskUsd: Decimal, entryOrderId: String, stopOrderId: String?, status: String) throws -> Position {
        var l = load()
        let p = Position(id: MockExchange.newId("pos"), symbol: symbol, side: side, quantity: Dec.str(quantity), entry: Dec.str(entry),
                         stop: Dec.str(stop), notional: Dec.str(notional), riskUsd: Dec.str(riskUsd), entryOrderId: entryOrderId,
                         stopOrderId: stopOrderId, status: status, time: Int64(Date().timeIntervalSince1970 * 1000))
        l.positions.append(p)
        try save(l)
        return p
    }

    public func flatten(_ symbol: String, side: Side, quantity: Decimal) async throws -> OrderResult {
        let mark = try await markPrice(symbol)
        var l = load()
        var remaining = quantity
        for i in l.positions.indices.reversed() where remaining > 0 {
            let p = l.positions[i]
            guard p.symbol == symbol, p.side == side, ["open", "unprotected"].contains(p.status) else { continue }
            let q = Dec.parse(p.quantity) ?? 0, e = Dec.parse(p.entry) ?? 0
            let closeQty = min(q, remaining)
            let pnl = side == .long ? (mark - e) * closeQty : (e - mark) * closeQty
            l.walletBalance = Dec.str((Dec.parse(l.walletBalance) ?? 0) + pnl)
            let left = q - closeQty
            if left <= 0 { l.positions[i].status = "flattened" }
            else { l.positions[i].quantity = Dec.str(left); l.positions[i].notional = Dec.str(left * e) }
            remaining -= closeQty
        }
        if remaining > 0 { throw ClientError("模拟平仓失败：找不到对应仓位", code: "FLATTEN_FAILED") }
        let o = Order(orderId: MockExchange.newId("mock-flat"), symbol: symbol, side: side.exitOrderSide, type: "MARKET",
                      status: "FILLED", avgPrice: Dec.str(mark), executedQty: Dec.str(quantity), reduceOnly: true)
        l.orders.append(o)
        try save(l)
        return OrderResult(orderId: o.orderId, avgPrice: mark, raw: ["orderId": o.orderId, "avgPrice": Dec.str(mark)])
    }

    public func positions() async throws -> [PositionInfo] {
        let l = load()
        let m = try await marks(for: l)
        return l.positions.filter { ["open", "unprotected"].contains($0.status) }.map { p in
            let q = Dec.parse(p.quantity) ?? 0, e = Dec.parse(p.entry) ?? 0, mk = m[p.symbol] ?? e
            let prot = p.status == "open" && (p.stopOrderId?.isEmpty == false)
            return PositionInfo(id: p.id, symbol: p.symbol, side: p.side, quantity: q, entry: e, mark: mk,
                                unrealizedPnl: p.side == .long ? (mk - e) * q : (e - mk) * q,
                                stop: Dec.parse(p.stop), stopOrderId: p.stopOrderId, protected: prot)
        }
    }

    /// 模拟止损触发：mark 穿过止损（多：mark<=stop；空：mark>=stop）即按该 mark 平仓记账。
    /// 这是模拟记账规则，不代表实盘成交价。
    public func applyStopTriggers(marks given: [String: Decimal]? = nil) async throws -> [StopHit] {
        var l = load()
        let m: [String: Decimal]
        if let given { m = given } else { m = try await marks(for: l) }
        var hits: [StopHit] = []
        for i in l.positions.indices {
            let p = l.positions[i]
            guard p.status == "open", p.stopOrderId?.isEmpty == false, let stop = Dec.parse(p.stop), let mark = m[p.symbol] else { continue }
            let hit = (p.side == .long && mark <= stop) || (p.side == .short && mark >= stop)
            guard hit else { continue }
            let q = Dec.parse(p.quantity) ?? 0, e = Dec.parse(p.entry) ?? 0
            let pnl = p.side == .long ? (mark - e) * q : (e - mark) * q
            let wallet = (Dec.parse(l.walletBalance) ?? 0) + pnl
            l.walletBalance = Dec.str(wallet)
            l.positions[i].status = "stopped"
            l.positions[i].closePrice = Dec.str(mark)
            l.positions[i].realizedPnl = Dec.str(pnl)
            l.positions[i].closeTime = Int64(Date().timeIntervalSince1970 * 1000)
            l.positions[i].closeReason = "stop_triggered"
            if let idx = l.orders.firstIndex(where: { $0.orderId == p.stopOrderId }) {
                l.orders[idx].status = "FILLED"; l.orders[idx].avgPrice = Dec.str(mark); l.orders[idx].executedQty = Dec.str(q)
            }
            hits.append(StopHit(symbol: p.symbol, side: p.side, quantity: q, entry: e, stop: stop, fillPrice: mark, realizedPnl: pnl, walletBalance: wallet))
        }
        if !hits.isEmpty { try save(l) }
        return hits
    }
}
