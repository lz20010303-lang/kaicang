import Foundation
import SwiftUI
import ControllerCore

/// App 状态。所有风控都在 ControllerCore 里执行，这里只负责把数据显示出来、把三项操作交给核心。
@MainActor
final class AppModel: ObservableObject {
    // 模式与凭据
    @Published private(set) var mode: TradingMode = .mock
    @Published private(set) var liveArmed = false
    @Published private(set) var maskedKeys: [TradingMode: String] = [:]

    // 交易对与自选
    @Published private(set) var watchlist: [String] = []
    @Published var symbol: String = "BTCUSDT"

    // 三项操作的输入
    @Published var side: Side? = nil { didSet { if oldValue != side { onInputsChanged(refill: true) } } }
    @Published var stopText: String = ""
    @Published var interval: Interval = .h1 { didSet { if oldValue != interval { onInputsChanged(refill: true); Task { await refreshMacd() } } } }
    /// nil = 手动填写；.ma / .supertrend = 按当前周期指标自动填入。
    @Published var stopSource: StopSource? = nil { didSet { if oldValue != stopSource { onInputsChanged(refill: true) } } }

    // 显示数据
    @Published private(set) var mark: Decimal?
    @Published private(set) var balance: Decimal?
    @Published private(set) var balanceField: String = ""
    @Published private(set) var filters: SymbolFilters?
    @Published private(set) var decision: Decision?
    @Published private(set) var stopFillReason: String?
    @Published private(set) var macd: MacdAnalysis?
    @Published private(set) var positions: [PositionInfo] = []
    @Published private(set) var logs: [OpLogEntry] = []
    @Published private(set) var statusLine: String = ""
    @Published private(set) var lastOutcome: OpenOutcome?
    @Published private(set) var sending = false
    @Published var toast: String?

    let market = PublicMarket()
    let credentials: CredentialStore
    let dataDir: URL
    let watchStore: WatchlistStore
    let oplog: OpLog
    private var controller: OpenController?
    private var loopTask: Task<Void, Never>?
    private var lastAnalysisAt = Date.distantPast

    init() {
        #if canImport(Security)
        credentials = KeychainCredentialStore()
        #else
        credentials = InMemoryCredentialStore()
        #endif
        let base = (try? FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true))
            ?? FileManager.default.temporaryDirectory
        dataDir = base.appendingPathComponent("OpenController", isDirectory: true)
        try? FileManager.default.createDirectory(at: dataDir, withIntermediateDirectories: true)
        watchStore = WatchlistStore(url: dataDir.appendingPathComponent("watchlist.json"))
        oplog = OpLog(url: dataDir.appendingPathComponent("ops.jsonl"))

        // 实盘开关不跨启动保存：上次停在实盘的话，这次从模拟开始。
        let saved = TradingMode(rawValue: UserDefaults.standard.string(forKey: "mode") ?? "mock") ?? .mock
        mode = saved == .live ? .mock : saved
        watchlist = watchStore.load()
        symbol = watchlist.first ?? "BTCUSDT"
        reloadMaskedKeys()
        rebuildController()
    }

    // MARK: - 生命周期

    func start() {
        guard loopTask == nil else { return }
        loopTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.tick()
                try? await Task.sleep(nanoseconds: 5_000_000_000)
            }
        }
    }

    func stop() { loopTask?.cancel(); loopTask = nil }

    private func tick() async {
        if let c = controller, mode == .mock {
            let hits = await c.sweepMockStops()
            if !hits.isEmpty { toast = "模拟止损触发 \(hits.count) 笔，已记账" }
        }
        await refreshMarket()
        if Date().timeIntervalSince(lastAnalysisAt) > 60 {
            lastAnalysisAt = Date()
            await refreshMacd()
            if stopSource != nil { await applyIndicator() }
        }
    }

    func refreshAll() async {
        await refreshMarket()
        await refreshMacd()
        if stopSource != nil { await applyIndicator() }
        lastAnalysisAt = Date()
    }

    // MARK: - 行情 / 账户 / 预览

    func refreshMarket() async {
        logs = oplog.read(limit: 50)
        guard let controller else {
            decision = nil; balance = nil; positions = []
            if let m = try? await market.markPrice(symbol) { mark = m }
            statusLine = "\(mode.zh)模式需要先在设置里填写自己的 API Key 与 Secret"
            return
        }
        let p = await controller.preview(symbol: symbol, side: side, stop: stopText)
        mark = p.mark; balance = p.balance; balanceField = p.balanceField ?? ""; filters = p.filters
        decision = p.decision
        do {
            positions = try await controller.exchange.positions()
            statusLine = ""
        } catch {
            statusLine = "读取持仓失败：\((error as? ClientError)?.message ?? "\(error)")"
        }
    }

    func refreshPreviewOnly() {
        guard mark != nil else { return }
        decision = RiskEngine.requireInWatchlist(symbol, watchlist)
            ?? RiskEngine.evaluate(side: side?.rawValue, stop: stopText, mark: mark, balance: balance, filters: filters)
    }

    func refreshMacd() async {
        macd = await Analysis.macd(market: market, symbol: symbol, interval: interval)
    }

    private func onInputsChanged(refill: Bool) {
        refreshPreviewOnly()
        if refill && stopSource != nil { Task { await applyIndicator() } }
        if stopSource == nil { stopFillReason = nil }
    }

    /// 选中「均线」或「Supertrend」时：按当前周期、当前方向自动填入止损；不满足条件就清空并说明原因。
    func applyIndicator() async {
        guard let source = stopSource else { return }
        var m: Decimal? = nil
        if let ex = controller?.exchange { m = try? await ex.markPrice(symbol) }
        if m == nil { m = try? await market.markPrice(symbol) }
        let tick = filters?.tickSize
        let fill = await Analysis.stopFill(market: market, symbol: symbol, interval: interval, source: source, side: side, mark: m, tickSize: tick)
        guard stopSource == source else { return }
        stopText = fill.fill.map { Dec.str($0) } ?? ""
        stopFillReason = fill.reason
        refreshPreviewOnly()
    }

    /// 用户手动改止损时调用：切回「手动」。
    func userEditedStop(_ text: String) {
        stopText = text
        if stopSource != nil { stopSource = nil }
        refreshPreviewOnly()
    }

    // MARK: - 开单（唯一的下单入口）

    func open() async {
        guard let controller, !sending else { return }
        sending = true
        defer { sending = false }
        let outcome = await controller.open(symbol: symbol, side: side, stop: stopText)
        lastOutcome = outcome
        toast = outcome.reasonZh
        await refreshMarket()
    }

    // MARK: - 自选

    func selectSymbol(_ s: String) {
        guard watchlist.contains(s) else { return }
        symbol = s
        stopText = ""; stopFillReason = nil
        mark = nil; filters = nil; decision = nil; macd = nil
        Task { await refreshAll() }
    }

    func addToWatchlist(_ raw: String) async {
        let r = await watchStore.add(raw, market: market)
        watchlist = r.symbols
        oplog.append(mode: mode, event: r.ok ? "watchlist_add" : "watchlist_reject", symbol: raw.uppercased(), reasonZh: r.reasonZh)
        toast = r.reasonZh
        logs = oplog.read(limit: 50)
    }

    func removeFromWatchlist(_ s: String) {
        do {
            watchlist = try watchStore.remove(s)
            oplog.append(mode: mode, event: "watchlist_remove", symbol: s, reasonZh: "已移出自选")
            if symbol == s, let first = watchlist.first { selectSymbol(first) }
            refreshPreviewOnly()
        } catch { toast = "保存自选失败" }
        logs = oplog.read(limit: 50)
    }

    // MARK: - 设置

    func setMode(_ m: TradingMode) {
        guard m != mode else { return }
        if m != .live { liveArmed = false }
        mode = m
        UserDefaults.standard.set(m.rawValue, forKey: "mode")
        oplog.append(mode: m, event: "mode", reasonZh: "切换到\(m.zh)模式")
        rebuildController()
        Task { await refreshAll() }
    }

    /// 实盘二次确认：必须先有实盘 Key，并输入「确认实盘」。只在本次运行有效。
    func armLive(confirmText: String) -> String? {
        guard mode == .live else { return "请先把模式切到实盘" }
        guard credentials.load(mode: .live) != nil else { return "请先填写实盘 API Key 与 Secret" }
        guard confirmText.trimmingCharacters(in: .whitespaces) == "确认实盘" else { return "请输入「确认实盘」四个字" }
        liveArmed = true
        oplog.append(mode: .live, event: "live_armed", reasonZh: "已二次确认开启实盘（本次运行有效）")
        rebuildController()
        return nil
    }

    func disarmLive() {
        liveArmed = false
        rebuildController()
    }

    func saveKeys(apiKey: String, secret: String, for m: TradingMode) -> String {
        let k = apiKey.trimmingCharacters(in: .whitespacesAndNewlines), s = secret.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !k.isEmpty, !s.isEmpty else { return "Key 和 Secret 都要填" }
        do {
            try credentials.save(APICredentials(apiKey: k, secret: s), mode: m)
            reloadMaskedKeys(); rebuildController()
            oplog.append(mode: m, event: "keys_saved", reasonZh: "已保存\(m.zh) API（Key 尾号 \(String(k.suffix(4)))）")
            Task { await refreshAll() }
            return "已保存到钥匙串"
        } catch { return "保存失败：\((error as? ClientError)?.message ?? "\(error)")" }
    }

    func deleteKeys(for m: TradingMode) {
        try? credentials.delete(mode: m)
        if m == .live { liveArmed = false }
        reloadMaskedKeys(); rebuildController()
        oplog.append(mode: m, event: "keys_deleted", reasonZh: "已删除\(m.zh) API")
    }

    func verifyKeys(for m: TradingMode) async -> String {
        guard let c = credentials.load(mode: m) else { return "还没有保存\(m.zh) API" }
        do {
            let ex = try BinanceSignedExchange(mode: m, apiKey: c.apiKey, secret: c.secret)
            let usdt = try await ex.verify()
            return "验证成功（只读）：USDT 余额 \(usdt ?? "-")"
        } catch { return "验证失败：\((error as? ClientError)?.message ?? "\(error)")" }
    }

    func resetMock() {
        guard let mock = controller?.exchange as? MockExchange else { return }
        try? mock.reset()
        oplog.append(mode: .mock, event: "mock_reset", reasonZh: "模拟账户已重置为 1000 USDT")
        Task { await refreshAll() }
    }

    private func reloadMaskedKeys() {
        var m: [TradingMode: String] = [:]
        for t in [TradingMode.testnet, .live] { if let c = credentials.load(mode: t) { m[t] = c.maskedKey } }
        maskedKeys = m
    }

    private func rebuildController() {
        switch mode {
        case .mock:
            let ex = MockExchange(ledgerURL: dataDir.appendingPathComponent("mock_ledger.json"), market: market)
            controller = OpenController(exchange: ex, watchlist: watchStore, oplog: oplog, liveArmed: false)
        case .testnet, .live:
            if let c = credentials.load(mode: mode), let ex = try? BinanceSignedExchange(mode: mode, apiKey: c.apiKey, secret: c.secret) {
                controller = OpenController(exchange: ex, watchlist: watchStore, oplog: oplog, liveArmed: mode == .live && liveArmed)
            } else {
                controller = nil
            }
        }
    }

    var canOpen: Bool {
        guard controller != nil, !sending, decision?.passed == true else { return false }
        if mode == .live && !liveArmed { return false }
        return true
    }
}
