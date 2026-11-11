import Foundation

public enum TradingMode: String, CaseIterable, Codable, Sendable, Identifiable {
    case mock, testnet, live
    public var id: String { rawValue }
    public var zh: String {
        switch self { case .mock: return "模拟"; case .testnet: return "测试网"; case .live: return "实盘" }
    }
}

public enum Endpoints {
    /// 官方 U 本位行情（K 线、自选校验）始终用主网公开接口。
    public static let publicFapi = "https://fapi.binance.com"
    public static let liveFapi = "https://fapi.binance.com"
    /// 官方 USDⓈ-M General Info 给出的测试网 REST 地址。
    public static let testnetFapi = "https://demo-fapi.binance.com"
    public static func base(for mode: TradingMode) -> String {
        switch mode { case .mock, .live: return liveFapi; case .testnet: return testnetFapi }
    }
}

public struct PositionInfo: Equatable, Sendable, Identifiable, Codable {
    public var id: String
    public var symbol: String
    public var side: Side
    public var quantity: Decimal
    public var entry: Decimal
    public var mark: Decimal
    public var unrealizedPnl: Decimal
    public var stop: Decimal?
    public var stopOrderId: String?
    public var protected: Bool
    public var statusZh: String { protected ? "已保护" : "未保护" }
}

public struct OrderResult: @unchecked Sendable {
    public let orderId: String
    public let avgPrice: Decimal?
    public let raw: [String: Any]
    public init(orderId: String, avgPrice: Decimal?, raw: [String: Any]) {
        self.orderId = orderId; self.avgPrice = avgPrice; self.raw = raw
    }
    public func fillPrice(fallback: Decimal) -> Decimal {
        for key in ["avgPrice", "price"] {
            if let v = Dec.from(raw[key]), v > 0 { return v }
        }
        return fallback
    }
}

/// 交易所接口。模拟账本与币安签名客户端都实现它，开单流程只依赖这个协议。
public protocol Exchange: AnyObject, Sendable {
    var mode: TradingMode { get }
    func markPrice(_ symbol: String) async throws -> Decimal
    func balance() async throws -> (Decimal, String)
    func filters(_ symbol: String) async throws -> SymbolFilters
    /// 下单前：全仓 + 20 倍（以及单向持仓模式检查）。任何一步失败都必须抛错，不得开单。
    func ensureAccountSettings(_ symbol: String) async throws
    func marketOpen(_ symbol: String, side: Side, quantity: Decimal) async throws -> OrderResult
    /// reduce-only STOP_MARKET，按标记价格（MARK_PRICE）触发。
    func placeStop(_ symbol: String, side: Side, stop: Decimal, quantity: Decimal) async throws -> OrderResult
    func flatten(_ symbol: String, side: Side, quantity: Decimal) async throws -> OrderResult
    func positions() async throws -> [PositionInfo]
}

/// 公开行情：mark、exchangeInfo、K 线。无需 Key。
public final class PublicMarket: @unchecked Sendable {
    let http: HTTPTransport
    let base: String
    private var filterCache: [String: SymbolFilters] = [:]
    private let lock = NSLock()

    public init(http: HTTPTransport = URLSessionTransport(timeout: 8), base: String = Endpoints.publicFapi) {
        self.http = http; self.base = base
    }

    func get(_ path: String, _ params: [(String, String)]) async throws -> Any {
        let q = Signer.queryString(params)
        guard let url = URL(string: base + path + (q.isEmpty ? "" : "?" + q)) else { throw ClientError("URL 无效", code: "URL") }
        let resp: HTTPResponse
        do { resp = try await http.send(method: "GET", url: url, headers: [:]) }
        catch { throw ClientError("连接币安失败：\(error.localizedDescription)", code: "NETWORK") }
        let body = try? JSON.parse(resp.body)
        if resp.status >= 400 || resp.status == 0 {
            let dict = body as? [String: Any]
            let msg = (dict?["msg"] as? String) ?? "HTTP \(resp.status)"
            let code = dict?["code"].map { "\($0)" } ?? "\(resp.status)"
            throw ClientError("币安接口错误 (\(code)): \(msg)", code: code, httpStatus: resp.status)
        }
        guard let body else { throw ClientError("币安返回的数据无法解析", code: "PARSE") }
        return body
    }

    public func markPrice(_ symbol: String) async throws -> Decimal {
        let data = try await get("/fapi/v1/premiumIndex", [("symbol", symbol.uppercased())])
        guard let d = data as? [String: Any], let mark = Dec.from(d["markPrice"]), mark > 0 else {
            throw ClientError("标记价格无效", code: "INVALID_MARK")
        }
        return mark
    }

    public func exchangeInfo(_ symbol: String) async throws -> [String: Any] {
        let data = try await get("/fapi/v1/exchangeInfo", [("symbol", symbol.uppercased())])
        guard let d = data as? [String: Any] else { throw ClientError("exchangeInfo 无效", code: "FILTERS") }
        return d
    }

    public func filters(_ symbol: String) async throws -> SymbolFilters {
        let key = symbol.uppercased()
        if let f = lock.locked({ filterCache[key] }) { return f }
        let info = try await exchangeInfo(key)
        guard let f = ExchangeInfo.parseFilters(info, symbol: key) else { throw ClientError("无法读取合约过滤器", code: "FILTERS") }
        lock.locked { filterCache[key] = f }
        return f
    }

    /// GET /fapi/v1/klines —— 官方 U 本位 K 线（主网公开数据）。
    public func klines(_ symbol: String, interval: String, limit: Int = IndicatorParams.klineLimit) async throws -> [Any] {
        let data: Any
        do {
            data = try await get("/fapi/v1/klines", [("symbol", symbol.uppercased()), ("interval", interval), ("limit", String(limit))])
        } catch let e as ClientError {
            throw ClientError("获取 K 线失败：\(e.message)", code: "KLINES")
        }
        guard let rows = data as? [Any], !rows.isEmpty else { throw ClientError("K 线数据为空或无效", code: "KLINES") }
        return rows
    }

    public func serverTime() async throws -> Int64 {
        let data = try await get("/fapi/v1/time", [])
        guard let d = data as? [String: Any], let t = d["serverTime"].flatMap(Candle.int64) else {
            throw ClientError("无法读取服务器时间", code: "TIME")
        }
        return t
    }
}

public enum ExchangeInfo {
    /// 与 Python `_parse_filters` 一致：PRICE_FILTER / LOT_SIZE / MARKET_LOT_SIZE / MIN_NOTIONAL。
    public static func parseFilters(_ info: [String: Any], symbol: String) -> SymbolFilters? {
        let target = symbol.uppercased()
        let symbols = info["symbols"] as? [[String: Any]] ?? []
        guard let entry = symbols.first(where: { ($0["symbol"] as? String) == target }) else { return nil }
        var tick: Decimal?, step: Decimal?, minQty: Decimal?, minNotional: Decimal?
        var marketStep: Decimal?, marketMin: Decimal?
        for f in entry["filters"] as? [[String: Any]] ?? [] {
            switch f["filterType"] as? String {
            case "PRICE_FILTER": tick = Dec.from(f["tickSize"])
            case "LOT_SIZE": step = Dec.from(f["stepSize"]); minQty = Dec.from(f["minQty"])
            case "MARKET_LOT_SIZE": marketStep = Dec.from(f["stepSize"]); marketMin = Dec.from(f["minQty"])
            case "MIN_NOTIONAL", "NOTIONAL": minNotional = Dec.from(f["notional"]) ?? Dec.from(f["minNotional"])
            default: break
            }
        }
        if step == nil, let ms = marketStep { step = ms }
        if let mm = marketMin, mm > 0, minQty == nil || mm > minQty! { minQty = mm }
        return SymbolFilters(tickSize: tick, stepSize: step, minQty: minQty, minNotional: minNotional)
    }

    public static func isSymbolCode(_ s: String) -> Bool {
        !s.isEmpty && s.unicodeScalars.allSatisfy { ($0.isASCII && CharacterSet.alphanumerics.contains($0)) || $0 == "_" || $0 == "-" }
    }

    /// 自选只能加 U 本位永续（USDT 计价、PERPETUAL、TRADING）。
    public static func classify(_ symbol: String, info: [String: Any]) -> (ok: Bool, reasonZh: String) {
        let target = symbol.trimmingCharacters(in: .whitespaces).uppercased()
        guard isSymbolCode(target) else { return (false, "交易对代码无效") }
        let symbols = info["symbols"] as? [[String: Any]] ?? []
        guard let entry = symbols.first(where: { (($0["symbol"] as? String) ?? "").uppercased() == target }) else {
            return (false, "未找到该交易对，请确认是币安 U 本位永续合约代码")
        }
        let contract = ((entry["contractType"] as? String) ?? "").uppercased()
        let quote = ((entry["quoteAsset"] as? String) ?? "").uppercased()
        let status = ((entry["status"] as? String) ?? "").uppercased()
        if contract != "PERPETUAL" || quote != "USDT" { return (false, "该交易对不是 U 本位永续合约，无法加入自选") }
        if !status.isEmpty && status != "TRADING" { return (false, "该交易对当前不可交易，无法加入自选") }
        return (true, "ok")
    }
}

extension NSLock {
    func locked<T>(_ body: () throws -> T) rethrows -> T { lock(); defer { unlock() }; return try body() }
}
