import Foundation
@testable import ControllerCore

/// 假网络：按路径返回固定 JSON，记录每次请求。
final class FakeTransport: HTTPTransport, @unchecked Sendable {
    struct Call { let method: String; let url: URL; let headers: [String: String] }
    var routes: [String: (Int, String)] = [:]
    var calls: [Call] = []
    let lock = NSLock()

    func send(method: String, url: URL, headers: [String: String]) async throws -> HTTPResponse {
        handle(method: method, url: url, headers: headers)
    }

    func handle(method: String, url: URL, headers: [String: String]) -> HTTPResponse {
        lock.lock(); defer { lock.unlock() }
        calls.append(Call(method: method, url: url, headers: headers))
        let key = "\(method) \(url.path)"
        guard let (status, body) = routes[key] ?? routes[url.path] else {
            return HTTPResponse(status: 404, body: Data("{\"code\":-1,\"msg\":\"no route \(key)\"}".utf8))
        }
        return HTTPResponse(status: status, body: Data(body.utf8))
    }

    func query(_ call: Call) -> [String: String] {
        var out: [String: String] = [:]
        for part in (call.url.query ?? "").split(separator: "&") {
            let kv = part.split(separator: "=", maxSplits: 1).map(String.init)
            out[kv[0]] = kv.count > 1 ? kv[1] : ""
        }
        return out
    }
}

func tmpDir() -> URL {
    let u = FileManager.default.temporaryDirectory.appendingPathComponent("oc-tests-\(UUID().uuidString)")
    try? FileManager.default.createDirectory(at: u, withIntermediateDirectories: true)
    return u
}

let btcFilters = SymbolFilters(tickSize: Decimal(string: "0.1"), stepSize: Decimal(string: "0.001"),
                               minQty: Decimal(string: "0.001"), minNotional: Decimal(string: "100"))!

func D(_ s: String) -> Decimal { Decimal(string: s)! }

/// 可控的交易所桩：用于止损失败、平仓失败等路径。
final class StubExchange: Exchange, @unchecked Sendable {
    let mode: TradingMode
    var mark: Decimal = 100
    var marks: [Decimal] = []        // 依次返回（模拟发送前价格变化）
    var bal: Decimal = 1000
    var failSettings = false, failEntry = false, failStop = false, failFlatten = false
    var log: [String] = []
    init(mode: TradingMode = .testnet) { self.mode = mode }
    func markPrice(_ symbol: String) async throws -> Decimal { if !marks.isEmpty { return marks.removeFirst() }; return mark }
    func balance() async throws -> (Decimal, String) { (bal, "stub") }
    func filters(_ symbol: String) async throws -> SymbolFilters { SymbolFilters(tickSize: D("0.01"), stepSize: D("0.001"), minQty: D("0.001"), minNotional: D("5"))! }
    func ensureAccountSettings(_ symbol: String) async throws { log.append("settings"); if failSettings { throw ClientError("x", code: "S") } }
    func marketOpen(_ symbol: String, side: Side, quantity: Decimal) async throws -> OrderResult {
        log.append("open \(side.rawValue) \(Dec.str(quantity))"); if failEntry { throw ClientError("entry", code: "E") }
        return OrderResult(orderId: "1", avgPrice: mark, raw: ["avgPrice": Dec.str(mark)])
    }
    func placeStop(_ symbol: String, side: Side, stop: Decimal, quantity: Decimal) async throws -> OrderResult {
        log.append("stop \(Dec.str(stop)) \(Dec.str(quantity))"); if failStop { throw ClientError("stop rejected", code: "-2021") }
        return OrderResult(orderId: "s1", avgPrice: nil, raw: [:])
    }
    func flatten(_ symbol: String, side: Side, quantity: Decimal) async throws -> OrderResult {
        log.append("flatten \(Dec.str(quantity))"); if failFlatten { throw ClientError("flat", code: "F") }
        return OrderResult(orderId: "f1", avgPrice: mark, raw: [:])
    }
    func positions() async throws -> [PositionInfo] { [] }
}
