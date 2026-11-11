import Foundation

public struct OpenOutcome: Sendable {
    public enum Status: String, Sendable { case pass = "PASS", reject = "REJECT" }
    public var status: Status
    public var opened: Bool
    public var reasonCode: String
    public var reasonZh: String
    public var decision: Decision?
    public var unprotected: Bool = false
    public var entryOrderId: String? = nil
    public var stopOrderId: String? = nil
    public var entryPrice: Decimal? = nil
}

public struct Preview: Sendable {
    public var symbol: String
    public var mark: Decimal?
    public var balance: Decimal?
    public var balanceField: String?
    public var filters: SymbolFilters?
    public var decision: Decision
}

/// 开单流程。顺序与 Python `_open_position` 一致：
/// 实盘开关 → 发送前取最新 mark/余额/过滤器 → 自选门槛 → 风控判定 → 全仓+20x → 市价开仓 →
/// reduce-only STOP_MARKET(MARK_PRICE) → 止损失败则立即市价平掉新仓；平仓也失败则报「未受保护」。
public final class OpenController: @unchecked Sendable {
    public let exchange: Exchange
    public let watchlist: WatchlistStore
    public let oplog: OpLog
    /// 实盘需要用户在设置里二次确认后才为 true；只在本次运行有效。
    public let liveArmed: Bool
    private var inFlight = false
    private let lock = NSLock()

    public init(exchange: Exchange, watchlist: WatchlistStore, oplog: OpLog, liveArmed: Bool) {
        self.exchange = exchange; self.watchlist = watchlist; self.oplog = oplog; self.liveArmed = liveArmed
    }

    var mode: TradingMode { exchange.mode }

    /// 预览：只算不下单。
    public func preview(symbol: String, side: Side?, stop: String) async -> Preview {
        let sym = symbol.uppercased()
        var p = Preview(symbol: sym, mark: nil, balance: nil, balanceField: nil, filters: nil,
                        decision: .reject("INVALID_INPUT", "输入数据缺失或无效（余额或标记价格）"))
        do {
            p.mark = try await exchange.markPrice(sym)
            let (b, field) = try await exchange.balance()
            p.balance = b; p.balanceField = field
            p.filters = try await exchange.filters(sym)
        } catch {
            p.decision = .reject("DATA_UNAVAILABLE", "读取行情或账户失败：\((error as? ClientError)?.message ?? "\(error)")", mark: p.mark)
            return p
        }
        if let gate = RiskEngine.requireInWatchlist(sym, watchlist.load()) { p.decision = gate; return p }
        p.decision = RiskEngine.evaluate(side: side?.rawValue, stop: stop, mark: p.mark, balance: p.balance, filters: p.filters)
        return p
    }

    func reject(_ code: String, _ zh: String, symbol: String, side: Side?, stop: Decimal? = nil, decision: Decision? = nil) -> OpenOutcome {
        oplog.append(mode: mode, event: "reject", symbol: symbol, side: side?.rawValue, stop: stop, reasonCode: code, reasonZh: zh)
        return OpenOutcome(status: .reject, opened: false, reasonCode: code, reasonZh: zh, decision: decision)
    }

    public func open(symbol: String, side: Side?, stop: String) async -> OpenOutcome {
        let sym = symbol.trimmingCharacters(in: .whitespaces).uppercased()
        if mode == .live && !liveArmed {
            return reject("LIVE_NOT_ARMED", "实盘未在设置里二次确认开启，拒绝下单", symbol: sym, side: side)
        }
        let busy: Bool = lock.locked { if inFlight { return true }; inFlight = true; return false }
        if busy { return OpenOutcome(status: .reject, opened: false, reasonCode: "IN_FLIGHT", reasonZh: "上一笔开单还在处理中，请稍候", decision: nil) }
        defer { lock.locked { inFlight = false } }

        // 发送前：用最新 mark / 余额 / 过滤器重新跑同一套硬规则（含自选门槛）。
        let mark: Decimal, balance: Decimal, filters: SymbolFilters
        do {
            mark = try await exchange.markPrice(sym)
            balance = try await exchange.balance().0
            filters = try await exchange.filters(sym)
        } catch {
            return reject("DATA_UNAVAILABLE", "读取最新行情或账户失败，未下单：\((error as? ClientError)?.message ?? "\(error)")", symbol: sym, side: side)
        }
        let decision = RiskEngine.requireInWatchlist(sym, watchlist.load())
            ?? RiskEngine.evaluate(side: side?.rawValue, stop: stop, mark: mark, balance: balance, filters: filters)
        guard decision.passed, let dSide = decision.side, let qty = decision.quantity, let dStop = decision.stop else {
            return reject(decision.reasonCode, decision.reasonZh, symbol: sym, side: decision.side ?? side, stop: decision.stop, decision: decision)
        }

        do { try await exchange.ensureAccountSettings(sym) }
        catch {
            return reject((error as? ClientError)?.code ?? "ACCOUNT_SETTINGS",
                          "设置全仓/20 倍失败，未下单：\((error as? ClientError)?.message ?? "\(error)")", symbol: sym, side: dSide, stop: dStop, decision: decision)
        }

        let entryOrder: OrderResult
        do { entryOrder = try await exchange.marketOpen(sym, side: dSide, quantity: qty) }
        catch {
            let e = error as? ClientError
            return reject(e?.code ?? "ENTRY_FAILED", "开仓下单失败：\(e?.message ?? "\(error)")", symbol: sym, side: dSide, stop: dStop, decision: decision)
        }
        let entry = entryOrder.fillPrice(fallback: decision.mark ?? mark)
        oplog.append(mode: mode, event: "open", symbol: sym, side: dSide.rawValue, quantity: qty, stop: dStop, price: entry, reasonZh: "开仓已成交")

        let stopOrder: OrderResult
        do {
            stopOrder = try await exchange.placeStop(sym, side: dSide, stop: dStop, quantity: qty)
        } catch {
            let stopMsg = (error as? ClientError)?.message ?? "\(error)"
            var flattenOK = false, flattenErr = ""
            do { _ = try await exchange.flatten(sym, side: dSide, quantity: qty); flattenOK = true }
            catch { flattenErr = (error as? ClientError)?.message ?? "\(error)" }
            if let mock = exchange as? MockExchange {
                _ = try? mock.recordPosition(symbol: sym, side: dSide, quantity: qty, entry: entry, stop: dStop, notional: decision.notional ?? 0,
                                         riskUsd: decision.riskUsd ?? 0, entryOrderId: entryOrder.orderId, stopOrderId: nil,
                                         status: flattenOK ? "flattened" : "unprotected")
            }
            oplog.append(mode: mode, event: "stop_failed", symbol: sym, side: dSide.rawValue, quantity: qty, stop: dStop, reasonZh: "保护性止损挂单失败：\(stopMsg)")
            if flattenOK {
                oplog.append(mode: mode, event: "flatten", symbol: sym, side: dSide.rawValue, quantity: qty, reasonZh: "止损失败后已按失败路径市价平掉新仓")
                return OpenOutcome(status: .reject, opened: false, reasonCode: "STOP_FAILED_FLATTENED",
                                   reasonZh: "开仓已成交，但止损单失败，已按失败路径市价平掉新仓，仓位不视为受保护。 止损错误：\(stopMsg)",
                                   decision: decision, unprotected: false, entryOrderId: entryOrder.orderId, entryPrice: entry)
            }
            oplog.append(mode: mode, event: "flatten", symbol: sym, side: dSide.rawValue, quantity: qty, reasonCode: "FLATTEN_FAILED", reasonZh: "止损失败后平仓也失败：\(flattenErr)")
            return OpenOutcome(status: .reject, opened: false, reasonCode: "STOP_FAILED_UNPROTECTED",
                               reasonZh: "开仓已成交，但止损单失败，随后平仓也失败。仓位目前未受保护，请立即人工处理。 止损错误：\(stopMsg)；平仓错误：\(flattenErr)",
                               decision: decision, unprotected: true, entryOrderId: entryOrder.orderId, entryPrice: entry)
        }
        oplog.append(mode: mode, event: "stop_placed", symbol: sym, side: dSide.rawValue, quantity: qty, stop: dStop, reasonZh: "保护性止损已挂上（reduce-only，按标记价格触发）")
        if let mock = exchange as? MockExchange {
            _ = try? mock.recordPosition(symbol: sym, side: dSide, quantity: qty, entry: entry, stop: dStop, notional: decision.notional ?? 0,
                                     riskUsd: decision.riskUsd ?? 0, entryOrderId: entryOrder.orderId, stopOrderId: stopOrder.orderId, status: "open")
        }
        return OpenOutcome(status: .pass, opened: true, reasonCode: "PASS", reasonZh: "开仓成功，保护性止损已挂上", decision: decision,
                           entryOrderId: entryOrder.orderId, stopOrderId: stopOrder.orderId, entryPrice: entry)
    }

    /// 模拟模式：检查止损是否被触发并记账。
    public func sweepMockStops() async -> [MockExchange.StopHit] {
        guard let mock = exchange as? MockExchange, let hits = try? await mock.applyStopTriggers() else { return [] }
        for h in hits {
            oplog.append(mode: .mock, event: "stop_triggered", symbol: h.symbol, side: h.side.rawValue, quantity: h.quantity, stop: h.stop,
                         price: h.fillPrice, reasonZh: "模拟止损触发：按标记价 \(Dec.str(h.fillPrice)) 平仓，已实现盈亏 \(Dec.str(Dec.roundHalfEven(h.realizedPnl, scale: 4))) USDT")
        }
        return hits
    }
}

/// 指标与 MACD：只读官方主网公开 K 线，在本机计算。
public enum Analysis {
    public static func stopFill(market: PublicMarket, symbol: String, interval: Interval, source: StopSource, side: Side?,
                                mark: Decimal?, tickSize: Decimal?) async -> StopFill {
        let label = Indicators.paramsLabel(source)
        guard let mark else {
            return Indicators.suggestStop(interval: interval.rawValue, source: source, value: nil, mark: nil, side: side, tickSize: nil,
                                          paramsLabel: label, error: "读取标记价格或过滤器失败")
        }
        let rows: [Any]
        do { rows = try await market.klines(symbol, interval: interval.rawValue, limit: IndicatorParams.klineLimit) }
        catch {
            return Indicators.suggestStop(interval: interval.rawValue, source: source, value: nil, mark: mark, side: side, tickSize: tickSize,
                                          paramsLabel: label, error: (error as? ClientError)?.message ?? "获取 K 线失败")
        }
        let candles = Indicators.closedCandles(rows)
        if candles.isEmpty {
            return Indicators.suggestStop(interval: interval.rawValue, source: source, value: nil, mark: mark, side: side, tickSize: tickSize,
                                          paramsLabel: label, error: "没有已收盘的 K 线，无法计算指标（不会使用未收盘的当前 K 线）")
        }
        let c = Indicators.compute(candles, source: source)
        return Indicators.suggestStop(interval: interval.rawValue, source: source, value: c.value, mark: mark, side: side, tickSize: tickSize,
                                      candle: c.candle, paramsLabel: label, error: c.ok ? nil : c.reasonZh)
    }

    public static func macd(market: PublicMarket, symbol: String, interval: Interval) async -> MacdAnalysis {
        let lower = interval.lower
        do {
            async let a = market.klines(symbol, interval: interval.rawValue, limit: MACDParams.klineLimit)
            async let b = market.klines(symbol, interval: lower, limit: MACDParams.klineLimit)
            let (ra, rb) = try await (a, b)
            return MACD.analyze(intervalCandles: Indicators.closedCandles(ra), lowerCandles: Indicators.closedCandles(rb),
                                interval: interval.rawValue, lower: lower)
        } catch {
            return MACD.failure(interval: interval.rawValue, lower: lower, reason: (error as? ClientError)?.message ?? "获取 K 线失败")
        }
    }
}
