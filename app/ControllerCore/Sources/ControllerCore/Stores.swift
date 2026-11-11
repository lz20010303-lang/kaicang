import Foundation

/// 自选列表。只有已在自选里的交易对才能开单。
public final class WatchlistStore: @unchecked Sendable {
    public static let defaultSymbols = ["BTCUSDT"]
    public static let maxCount = 50
    public let url: URL
    private let lock = NSLock()
    public init(url: URL) { self.url = url }

    struct File: Codable { var symbols: [String] }

    public func load() -> [String] {
        lock.lock(); defer { lock.unlock() }
        guard let data = try? Data(contentsOf: url) else { return WatchlistStore.defaultSymbols }
        guard let f = try? JSONDecoder().decode(File.self, from: data) else { return WatchlistStore.defaultSymbols }
        var seen = Set<String>(), out: [String] = []
        for raw in f.symbols.prefix(WatchlistStore.maxCount) {
            let s = raw.trimmingCharacters(in: .whitespaces).uppercased()
            if !s.isEmpty && !seen.contains(s) { seen.insert(s); out.append(s) }
        }
        return out
    }

    func save(_ symbols: [String]) throws {
        lock.lock(); defer { lock.unlock() }
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try JSONEncoder().encode(File(symbols: symbols)).write(to: url, options: .atomic)
    }

    /// 加入前先用币安公开 exchangeInfo 校验：必须是 U 本位永续、USDT 计价、可交易。
    public func add(_ symbol: String, market: PublicMarket) async -> (ok: Bool, reasonZh: String, symbols: [String]) {
        let sym = symbol.trimmingCharacters(in: .whitespaces).uppercased()
        var list = load()
        if list.contains(sym) { return (true, "已在自选中", list) }
        if list.count >= WatchlistStore.maxCount { return (false, "自选最多 \(WatchlistStore.maxCount) 个", list) }
        guard ExchangeInfo.isSymbolCode(sym) else { return (false, "交易对代码无效", list) }
        let info: [String: Any]
        do { info = try await market.exchangeInfo(sym) }
        catch {
            // 币安对不存在的代码返回 400 (-1121)
            if let e = error as? ClientError, e.code == "-1121" { return (false, "未找到该交易对，请确认是币安 U 本位永续合约代码", list) }
            return (false, "无法验证该交易对（网络或交易所接口不可用），未加入自选", list)
        }
        let r = ExchangeInfo.classify(sym, info: info)
        guard r.ok else { return (false, r.reasonZh, list) }
        list.append(sym)
        do { try save(list) } catch { return (false, "保存自选失败：\(error.localizedDescription)", load()) }
        return (true, "已加入自选", list)
    }

    public func remove(_ symbol: String) throws -> [String] {
        let sym = symbol.trimmingCharacters(in: .whitespaces).uppercased()
        let list = load().filter { $0 != sym }
        try save(list)
        return list
    }

    /// 测试/初始化用：直接写入（不做网络校验）。
    public func overwrite(_ symbols: [String]) throws { try save(symbols) }
}

public struct OpLogEntry: Codable, Equatable, Sendable, Identifiable {
    public var id: String
    public var ts: Int64
    public var mode: String
    public var event: String       // open / reject / stop_placed / stop_failed / flatten / stop_triggered / watchlist_add / ...
    public var symbol: String?
    public var side: String?
    public var quantity: String?
    public var stop: String?
    public var price: String?
    public var reasonCode: String?
    public var reasonZh: String

    public var timeLabel: String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX"); f.timeZone = Indicators.shanghai; f.dateFormat = "MM-dd HH:mm:ss"
        return f.string(from: Date(timeIntervalSince1970: TimeInterval(ts) / 1000))
    }
}

/// 操作日志（JSON Lines，追加写）。只记录本机发生的操作，不含任何 Key。
public final class OpLog: @unchecked Sendable {
    public let url: URL
    public static let maxLines = 2000
    private let lock = NSLock()
    public init(url: URL) { self.url = url }

    @discardableResult
    public func append(mode: TradingMode, event: String, symbol: String? = nil, side: String? = nil, quantity: Decimal? = nil,
                       stop: Decimal? = nil, price: Decimal? = nil, reasonCode: String? = nil, reasonZh: String) -> OpLogEntry {
        let e = OpLogEntry(id: UUID().uuidString, ts: Int64(Date().timeIntervalSince1970 * 1000), mode: mode.rawValue, event: event,
                           symbol: symbol, side: side, quantity: Dec.strOpt(quantity), stop: Dec.strOpt(stop), price: Dec.strOpt(price),
                           reasonCode: reasonCode, reasonZh: reasonZh)
        lock.lock(); defer { lock.unlock() }
        let enc = JSONEncoder(); enc.outputFormatting = [.sortedKeys]
        guard var line = try? enc.encode(e) else { return e }
        line.append(0x0A)
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        if let h = try? FileHandle(forWritingTo: url) {
            h.seekToEndOfFile(); h.write(line); h.closeFile()
        } else {
            try? line.write(to: url, options: .atomic)
        }
        return e
    }

    /// 最新在前。
    public func read(limit: Int = 50) -> [OpLogEntry] {
        lock.lock(); defer { lock.unlock() }
        guard let data = try? Data(contentsOf: url), let text = String(data: data, encoding: .utf8) else { return [] }
        let lines = text.split(separator: "\n")
        if lines.count > OpLog.maxLines * 2 {
            let keep = lines.suffix(OpLog.maxLines).joined(separator: "\n") + "\n"
            try? keep.data(using: .utf8)?.write(to: url, options: .atomic)
        }
        return lines.suffix(limit).compactMap { try? JSONDecoder().decode(OpLogEntry.self, from: Data($0.utf8)) }.reversed()
    }
}
