import Foundation
import ControllerCore

// 对拍工具：
//   parity-cli fixture <in.json> <out.json>   用同一组输入算出结果，交给 Tools/compare.py 与 Python 版比对
//   parity-cli live [SYMBOL]                   用币安公开 K 线做一次真实数据计算（不需要 Key、不下单）

func s(_ d: Decimal?) -> Any { d.map { Dec.str($0) } ?? NSNull() }
func o(_ v: String?) -> Any { v ?? NSNull() }

func filters(_ f: [String: Any]) -> SymbolFilters? {
    SymbolFilters(tickSize: Dec.from(f["tick"]), stepSize: Dec.from(f["step"]), minQty: Dec.from(f["min_qty"]), minNotional: Dec.from(f["min_notional"]))
}

func runFixture(_ inPath: String, _ outPath: String) throws {
    let data = try Data(contentsOf: URL(fileURLWithPath: inPath))
    let root = try JSONSerialization.jsonObject(with: data) as! [String: Any]
    var out: [String: Any] = [:]

    out["engine"] = (root["engine"] as? [[String: Any]] ?? []).map { c -> [String: Any] in
        let d = RiskEngine.evaluate(side: c["side"] as? String, stop: c["stop"] as? String, mark: Dec.from(c["mark"]),
                                    balance: Dec.from(c["balance"]), filters: filters(c["filters"] as? [String: Any] ?? [:]))
        return ["status": d.status, "reason_code": d.reasonCode, "reason_zh": d.reasonZh, "side": o(d.side?.rawValue),
                "stop": s(d.stop), "quantity": s(d.quantity), "raw_quantity": s(d.rawQuantity), "notional": s(d.notional), "risk_usd": s(d.riskUsd)]
    }

    out["suggest"] = (root["suggest"] as? [[String: Any]] ?? []).map { c -> [String: Any] in
        let side: Side? = (c["side"] as? String).flatMap { Side(rawValue: $0) }
        let f = Indicators.suggestStop(interval: c["interval"] as! String, source: StopSource(rawValue: c["source"] as! String)!,
                                       value: Dec.from(c["value"]), mark: Dec.from(c["mark"]), side: side, tickSize: Dec.from(c["tick"]),
                                       paramsLabel: c["params_label"] as? String ?? "")
        return ["fill": s(f.fill), "usable": f.usableForSide, "reason": f.reason]
    }

    out["series"] = (root["series"] as? [[String: Any]] ?? []).map { c -> [String: Any] in
        let now = (c["now_ms"] as? NSNumber)?.int64Value
        let candles = Indicators.closedCandles(c["rows"] as? [Any], nowMs: now)
        let lower = Indicators.closedCandles(c["lower_rows"] as? [Any], nowMs: now)
        let sma = Indicators.computeSMA(candles, period: IndicatorParams.maPeriod)
        let st = Indicators.computeSupertrend(candles, period: IndicatorParams.supertrendAtrPeriod, multiplier: IndicatorParams.supertrendMultiplier)
        let m = MACD.analyze(intervalCandles: candles, lowerCandles: lower, interval: c["interval"] as? String ?? "15m", lower: c["lower"] as? String ?? "5m")
        let label = Indicators.candleLabel(c["interval"] as? String ?? "15m", sma.candle)
        return ["name": c["name"] ?? "", "n_closed": candles.count, "sma": s(sma.value), "sma_reason": o(sma.reasonZh),
                "supertrend": s(st.value), "st_reason": o(st.reasonZh), "candle_label": o(label),
                "cross": ["type": o(m.cross.type), "type_zh": m.cross.typeZh, "bars_ago": m.cross.barsAgo.map { $0 as Any } ?? NSNull(),
                          "dif": o(m.cross.dif), "dea": o(m.cross.dea), "histogram": o(m.cross.histogram), "reason_zh": m.cross.reasonZh, "label_zh": m.cross.labelZh],
                "divergence": ["type": o(m.divergence.type), "type_zh": m.divergence.typeZh, "reason_zh": m.divergence.reasonZh, "label_zh": m.divergence.labelZh,
                               "last_max_dif": o(m.divergence.lastMaxDif), "last_min_dif": o(m.divergence.lastMinDif),
                               "prev_max_dif": o(m.divergence.prevMaxDif), "prev_min_dif": o(m.divergence.prevMinDif),
                               "last_price_high": o(m.divergence.lastPriceHigh), "prev_price_high": o(m.divergence.prevPriceHigh),
                               "last_price_low": o(m.divergence.lastPriceLow), "prev_price_low": o(m.divergence.prevPriceLow)],
                "macd_ok": m.ok, "macd_reason": m.reasonZh]
    }

    out["sign"] = (root["sign"] as? [[String: Any]] ?? []).map { c -> [String: Any] in
        ["sig": Signer.hmacSHA256Hex(secret: c["secret"] as! String, message: c["message"] as! String)]
    }
    let enc = try JSONSerialization.data(withJSONObject: out, options: [.prettyPrinted, .sortedKeys])
    try enc.write(to: URL(fileURLWithPath: outPath))
}

func runLive(_ symbol: String) async {
    let market = PublicMarket()
    do {
        let mark = try await market.markPrice(symbol)
        let f = try await market.filters(symbol)
        print("symbol=\(symbol) mark=\(Dec.str(mark)) tick=\(Dec.str(f.tickSize)) step=\(Dec.str(f.stepSize)) minQty=\(Dec.str(f.minQty)) minNotional=\(Dec.str(f.minNotional))")
        for iv in Interval.allCases {
            let ma = await Analysis.stopFill(market: market, symbol: symbol, interval: iv, source: .ma, side: nil, mark: mark, tickSize: f.tickSize)
            let st = await Analysis.stopFill(market: market, symbol: symbol, interval: iv, source: .supertrend, side: nil, mark: mark, tickSize: f.tickSize)
            let m = await Analysis.macd(market: market, symbol: symbol, interval: iv)
            print("[\(iv.label)] SMA20=\(s(ma.value)) fill=\(s(ma.fill)) | \(ma.reason)")
            print("[\(iv.label)] ST(10,3)=\(s(st.value)) fill=\(s(st.fill)) | \(st.reason)")
            print("[\(iv.label)] \(m.cross.labelZh) DIF=\(m.cross.dif ?? "-") DEA=\(m.cross.dea ?? "-") | \(m.divergence.labelZh)")
        }
        let d = RiskEngine.evaluate(side: "LONG", stop: Dec.str(mark * Decimal(string: "0.99")!), mark: mark, balance: 1000, filters: f)
        print("示例风控(余额1000, 多, 止损=mark×0.99): \(d.status) qty=\(s(d.quantity)) stop=\(s(d.stop)) risk=\(s(d.riskUsd)) notional=\(s(d.notional)) \(d.reasonZh)")
    } catch {
        print("失败：\(error)")
        exit(1)
    }
}

let args = CommandLine.arguments
if args.count >= 4 && args[1] == "fixture" {
    do { try runFixture(args[2], args[3]) } catch { print("fixture 失败：\(error)"); exit(1) }
} else if args.count >= 2 && args[1] == "live" {
    let sem = DispatchSemaphore(value: 0)
    Task { await runLive(args.count >= 3 ? args[2].uppercased() : "BTCUSDT"); sem.signal() }
    sem.wait()
} else {
    print("用法: parity-cli fixture <in.json> <out.json> | parity-cli live [SYMBOL]")
}
