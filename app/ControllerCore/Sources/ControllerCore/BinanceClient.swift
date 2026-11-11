import Foundation

/// 测试网 / 实盘签名客户端。Key 与 Secret 只在本机内存里用于签名，请求从本机直连币安。
public final class BinanceSignedExchange: Exchange, @unchecked Sendable {
    public let mode: TradingMode
    let http: HTTPTransport
    let base: String
    let apiKey: String
    let secret: String
    let market: PublicMarket
    private var timeOffsetMs: Int64? = nil
    private let lock = NSLock()

    public init(mode: TradingMode, apiKey: String, secret: String, http: HTTPTransport = URLSessionTransport(timeout: 10)) throws {
        guard mode != .mock else { throw ClientError("模拟模式不使用签名客户端", code: "MODE") }
        let k = apiKey.trimmingCharacters(in: .whitespacesAndNewlines)
        let s = secret.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !k.isEmpty, !s.isEmpty else { throw ClientError("请先在设置里填写 API Key 与 Secret", code: "NO_KEYS") }
        self.mode = mode; self.http = http; self.apiKey = k; self.secret = s
        self.base = Endpoints.base(for: mode)
        self.market = PublicMarket(http: http, base: base)
    }

    func timestamp() async -> Int64 {
        let local = Int64(Date().timeIntervalSince1970 * 1000)
        if let off = lock.locked({ timeOffsetMs }) { return local + off }
        if let server = try? await market.serverTime() {
            let after = Int64(Date().timeIntervalSince1970 * 1000)
            let off = server - (local + after) / 2
            lock.locked { timeOffsetMs = off }
            return after + off
        }
        return local
    }

    /// 发送请求。错误信息里绝不带 URL（URL 里有签名）。
    func request(_ method: String, _ path: String, _ params: [(String, String)] = [], signed: Bool = true) async throws -> Any {
        let query: String
        if signed {
            query = Signer.signedQuery(params, secret: secret, timestampMs: await timestamp())
        } else {
            query = Signer.queryString(params)
        }
        guard let url = URL(string: base + path + (query.isEmpty ? "" : "?" + query)) else {
            throw ClientError("URL 无效", code: "URL")
        }
        let resp: HTTPResponse
        do { resp = try await http.send(method: method, url: url, headers: ["X-MBX-APIKEY": apiKey]) }
        catch { throw ClientError("连接币安失败：\(type(of: error))", code: "NETWORK") }
        let body = try? JSON.parse(resp.body)
        if resp.status >= 400 || resp.status == 0 {
            let dict = body as? [String: Any]
            let msg = (dict?["msg"] as? String) ?? String(data: resp.body, encoding: .utf8) ?? "HTTP \(resp.status)"
            var code = dict?["code"].map { "\($0)" } ?? "\(resp.status)"
            if code == "0" { code = "\(resp.status)" }
            if code == "-1021" { lock.locked { timeOffsetMs = nil } }
            throw ClientError("币安接口错误 (\(code)): \(msg)", code: code, httpStatus: resp.status)
        }
        guard let body else { throw ClientError("币安返回的数据无法解析", code: "PARSE") }
        return body
    }

    /// 只读校验 Key（GET /fapi/v2/balance），不下单。
    public func verify() async throws -> String? {
        let rows = try await request("GET", "/fapi/v2/balance") as? [[String: Any]] ?? []
        return rows.first(where: { ($0["asset"] as? String) == "USDT" })?["balance"].map { "\($0)" }
    }

    public func markPrice(_ symbol: String) async throws -> Decimal { try await market.markPrice(symbol) }
    public func filters(_ symbol: String) async throws -> SymbolFilters { try await market.filters(symbol) }

    public func balance() async throws -> (Decimal, String) {
        guard let d = try await request("GET", "/fapi/v2/account") as? [String: Any] else {
            throw ClientError("账户信息无效", code: "NO_BALANCE")
        }
        if let v = Dec.from(d["totalMarginBalance"]) { return (v, "totalMarginBalance（保证金余额/权益）") }
        if let v = Dec.from(d["totalWalletBalance"]) { return (v, "totalWalletBalance（钱包余额）") }
        throw ClientError("账户信息中没有保证金余额", code: "NO_BALANCE")
    }

    public func ensureAccountSettings(_ symbol: String) async throws {
        // 1) 必须是单向持仓：双向持仓下 reduceOnly 不能发送，止损逻辑会失效。
        let dual = try await request("GET", "/fapi/v1/positionSide/dual") as? [String: Any]
        guard let flag = dual?["dualSidePosition"] else {
            throw ClientError("无法确认持仓模式，已停止开单", code: "POSITION_MODE")
        }
        if BinanceSignedExchange.truthy(flag) {
            throw ClientError("当前账户是双向持仓模式，本工具只支持单向持仓。请在币安合约设置里改为单向持仓后再开单", code: "HEDGE_MODE")
        }
        // 2) 全仓。-4046 = 已经是全仓。
        do {
            _ = try await request("POST", "/fapi/v1/marginType", [("symbol", symbol), ("marginType", RiskRules.marginMode)])
        } catch let e as ClientError {
            let text = e.message.lowercased()
            if !(e.code.contains("4046") || text.contains("no need to change") || text.contains("already")) { throw e }
        }
        // 3) 20 倍，并核对返回值。
        let lev = try await request("POST", "/fapi/v1/leverage", [("symbol", symbol), ("leverage", String(RiskRules.leverage))]) as? [String: Any]
        if let got = lev?["leverage"].flatMap({ Int("\($0)") }), got != RiskRules.leverage {
            throw ClientError("杠杆设置未生效（返回 \(got)x），已停止开单", code: "LEVERAGE")
        }
    }

    func order(_ any: Any, idKey: String = "orderId") -> OrderResult {
        let d = any as? [String: Any] ?? [:]
        return OrderResult(orderId: d[idKey].map { "\($0)" } ?? "", avgPrice: Dec.from(d["avgPrice"]), raw: d)
    }

    public func marketOpen(_ symbol: String, side: Side, quantity: Decimal) async throws -> OrderResult {
        order(try await request("POST", "/fapi/v1/order", [
            ("symbol", symbol), ("side", side.entryOrderSide), ("type", "MARKET"),
            ("quantity", Dec.str(quantity)), ("newOrderRespType", "RESULT"),
        ]))
    }

    /// 自 2025-12-09 起，币安 U 本位条件单（STOP_MARKET 等）必须走 Algo 接口：POST /fapi/v1/algoOrder。
    public func placeStop(_ symbol: String, side: Side, stop: Decimal, quantity: Decimal) async throws -> OrderResult {
        order(try await request("POST", "/fapi/v1/algoOrder", [
            ("algoType", "CONDITIONAL"), ("symbol", symbol), ("side", side.exitOrderSide), ("type", "STOP_MARKET"),
            ("triggerPrice", Dec.str(stop)), ("quantity", Dec.str(quantity)), ("reduceOnly", "true"),
            ("workingType", "MARK_PRICE"),
        ]), idKey: "algoId")
    }

    public func flatten(_ symbol: String, side: Side, quantity: Decimal) async throws -> OrderResult {
        order(try await request("POST", "/fapi/v1/order", [
            ("symbol", symbol), ("side", side.exitOrderSide), ("type", "MARKET"),
            ("quantity", Dec.str(quantity)), ("reduceOnly", "true"), ("newOrderRespType", "RESULT"),
        ]))
    }

    public func positions() async throws -> [PositionInfo] {
        let raw = try await request("GET", "/fapi/v2/positionRisk") as? [[String: Any]] ?? []
        let algo = (try? await request("GET", "/fapi/v1/openAlgoOrders")) as? [[String: Any]] ?? []
        let legacy = (try? await request("GET", "/fapi/v1/openOrders")) as? [[String: Any]] ?? []
        return BinanceSignedExchange.mapPositions(raw, algoOrders: algo, legacyOrders: legacy)
    }

    static func truthy(_ any: Any?) -> Bool {
        if let b = any as? Bool { return b }
        if let s = any as? String { return s.lowercased() == "true" }
        if let n = any as? NSNumber { return n.boolValue }
        return false
    }

    /// 把 positionRisk 与挂着的止损单配对，判断每个仓位是否受保护。
    public static func mapPositions(_ raw: [[String: Any]], algoOrders: [[String: Any]], legacyOrders: [[String: Any]]) -> [PositionInfo] {
        var out: [PositionInfo] = []
        for item in raw {
            guard let amt = Dec.from(item["positionAmt"]), amt != 0 else { continue }
            let side: Side = amt > 0 ? .long : .short
            let qty = abs(amt)
            let symbol = item["symbol"] as? String ?? ""
            let entry = Dec.from(item["entryPrice"]) ?? 0
            let mark = Dec.from(item["markPrice"]) ?? 0
            let upnl = Dec.from(item["unRealizedProfit"]) ?? (side == .long ? (mark - entry) * qty : (entry - mark) * qty)
            var stop: Decimal? = nil, stopId: String? = nil
            for o in algoOrders where (o["symbol"] as? String) == symbol {
                let typ = ((o["orderType"] as? String) ?? (o["type"] as? String) ?? "").uppercased()
                guard ["STOP", "STOP_MARKET", "TRAILING_STOP_MARKET"].contains(typ) else { continue }
                guard truthy(o["reduceOnly"]) || truthy(o["closePosition"]) else { continue }
                guard ((o["side"] as? String) ?? "").uppercased() == side.exitOrderSide else { continue }
                stop = Dec.from(o["triggerPrice"]); stopId = o["algoId"].map { "\($0)" }; break
            }
            if stopId == nil {
                for o in legacyOrders where (o["symbol"] as? String) == symbol {
                    let typ = ((o["type"] as? String) ?? "").uppercased()
                    guard ["STOP", "STOP_MARKET", "TRAILING_STOP_MARKET"].contains(typ) else { continue }
                    guard truthy(o["reduceOnly"]) || truthy(o["closePosition"]) else { continue }
                    guard ((o["side"] as? String) ?? "").uppercased() == side.exitOrderSide else { continue }
                    stop = Dec.from(o["stopPrice"]); stopId = o["orderId"].map { "\($0)" }; break
                }
            }
            out.append(PositionInfo(id: "\(symbol)-\(side.rawValue)", symbol: symbol, side: side, quantity: qty, entry: entry,
                                    mark: mark, unrealizedPnl: upnl, stop: stop, stopOrderId: stopId, protected: stopId != nil))
        }
        return out
    }
}
