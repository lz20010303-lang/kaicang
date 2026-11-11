import SwiftUI
import ControllerCore

struct ContentView: View {
    @EnvironmentObject var model: AppModel
    @State private var showSettings = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 14) {
                    HeaderView()
                    MacdStrip()
                    TradeCard()
                    WatchlistCard()
                    PositionsCard()
                    LogCard()
                }
                .padding()
            }
            .navigationTitle("开仓控制器")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button { showSettings = true } label: { Image(systemName: "gearshape") }
                }
            }
            .sheet(isPresented: $showSettings) { SettingsView().environmentObject(model) }
            .refreshable { await model.refreshAll() }
            .overlay(alignment: .bottom) { ToastView() }
        }
    }
}

struct ToastView: View {
    @EnvironmentObject var model: AppModel
    var body: some View {
        if let t = model.toast {
            Text(t)
                .font(.callout)
                .padding(12)
                .background(RoundedRectangle(cornerRadius: 10).fill(Color.black.opacity(0.85)))
                .foregroundColor(.white)
                .padding()
                .onTapGesture { model.toast = nil }
                .task(id: t) {
                    try? await Task.sleep(nanoseconds: 6_000_000_000)
                    if model.toast == t { model.toast = nil }
                }
        }
    }
}

struct HeaderView: View {
    @EnvironmentObject var model: AppModel
    var modeColor: Color {
        switch model.mode { case .mock: return .gray; case .testnet: return .orange; case .live: return .red }
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Pill(text: model.mode.zh + (model.mode == .live ? (model.liveArmed ? "·已确认" : "·未确认") : ""), color: modeColor, neutral: model.mode == .mock)
                Spacer()
                Text("全仓 · \(RiskRules.leverage)x · 单笔风险 5%").font(.caption).foregroundColor(.secondary)
            }
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading) {
                    Text("账户余额").font(.caption).foregroundColor(.secondary)
                    Text("\(Fmt.usd(model.balance)) USDT").font(.title2.monospacedDigit().weight(.semibold))
                    if !model.balanceField.isEmpty { Text(model.balanceField).font(.caption2).foregroundColor(.secondary) }
                }
                Spacer()
                VStack(alignment: .trailing) {
                    Text(model.symbol).font(.caption).foregroundColor(.secondary)
                    Text(Fmt.num(model.mark)).font(.title3.monospacedDigit())
                    Text("标记价格").font(.caption2).foregroundColor(.secondary)
                }
            }
            if !model.statusLine.isEmpty {
                Text(model.statusLine).font(.caption).foregroundColor(.red)
            }
        }
    }
}

/// 交易操作区上方的 MACD 参考标注：同向绿、反向红、无信号灰。仅供参考，不影响开单。
struct MacdStrip: View {
    @EnvironmentObject var model: AppModel
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                if let m = model.macd {
                    tonePill(m.cross.labelZh, MACD.tone(bias: m.cross.bias, side: model.side))
                    tonePill(m.divergence.labelZh, MACD.tone(bias: m.divergence.bias, side: model.side))
                } else {
                    Pill(text: "MACD 计算中…", color: .gray, neutral: true)
                }
            }
            Text("\(MACDParams.label) · 只用已收盘 K 线 · 仅供参考，不影响开单").font(.caption2).foregroundColor(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func tonePill(_ text: String, _ tone: MACD.Tone) -> some View {
        Pill(text: text, color: tone.color, neutral: tone == .gray)
    }
}

struct TradeCard: View {
    @EnvironmentObject var model: AppModel
    @State private var confirm = false

    var stopBinding: Binding<String> {
        Binding(get: { model.stopText }, set: { model.userEditedStop($0) })
    }
    var sourceBinding: Binding<String> {
        Binding(get: { model.stopSource?.rawValue ?? "manual" },
                set: { model.stopSource = StopSource(rawValue: $0) })
    }
    var sideBinding: Binding<String> {
        Binding(get: { model.side?.rawValue ?? "" }, set: { model.side = Side(rawValue: $0) })
    }

    var body: some View {
        Card(title: "交易操作 · \(model.symbol)") {
            Picker("周期", selection: $model.interval) {
                ForEach(Interval.allCases) { Text($0.label).tag($0) }
            }.pickerStyle(.segmented)

            Text("① 止损价（必填）").font(.subheadline.weight(.medium))
            Picker("止损来源", selection: sourceBinding) {
                Text("手动").tag("manual")
                Text("均线 \(IndicatorParams.maLabel)").tag(StopSource.ma.rawValue)
                Text("Supertrend").tag(StopSource.supertrend.rawValue)
            }.pickerStyle(.segmented)
            TextField("填写止损价", text: stopBinding)
                .textFieldStyle(.roundedBorder)
                .font(.body.monospacedDigit())
                #if os(iOS)
                .keyboardType(.decimalPad)
                #endif
            if let r = model.stopFillReason {
                Text(r).font(.caption).foregroundColor(model.stopText.isEmpty ? .orange : .secondary)
            }

            Text("② 方向").font(.subheadline.weight(.medium))
            Picker("方向", selection: sideBinding) {
                Text("未选").tag("")
                Text("做多").tag(Side.long.rawValue)
                Text("做空").tag(Side.short.rawValue)
            }.pickerStyle(.segmented)

            PreviewBox(decision: model.decision)

            Button {
                confirm = true
            } label: {
                HStack {
                    Spacer()
                    if model.sending { ProgressView() }
                    Text(model.sending ? "下单中…" : "③ 开单").font(.headline)
                    Spacer()
                }.padding(.vertical, 10)
            }
            .buttonStyle(.borderedProminent)
            .tint(model.side == .short ? .red : .green)
            .disabled(!model.canOpen)
            .confirmationDialog("确认开单", isPresented: $confirm, titleVisibility: .visible) {
                Button("确认\(model.side?.zh ?? "")（\(model.mode.zh)）") { Task { await model.open() } }
                Button("取消", role: .cancel) {}
            } message: {
                if let d = model.decision, d.passed {
                    Text("\(model.symbol) \(d.side?.zh ?? "") 数量 \(Fmt.num(d.quantity))，止损 \(Fmt.num(d.stop))，风险 \(Fmt.usd(d.riskUsd)) USDT。发送前会用最新标记价格再检查一遍。")
                }
            }
            if model.mode == .live && !model.liveArmed {
                Text("实盘未二次确认：请到设置里开启后才能开单").font(.caption).foregroundColor(.red)
            }
            if let o = model.lastOutcome, o.unprotected {
                Text("⚠️ \(o.reasonZh)").font(.callout.weight(.semibold)).foregroundColor(.red)
            }
        }
    }
}

struct PreviewBox: View {
    let decision: Decision?
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let d = decision {
                HStack {
                    Text(d.passed ? "可以开单" : "不能开单").font(.subheadline.weight(.semibold))
                        .foregroundColor(d.passed ? .green : .orange)
                    Spacer()
                    if !d.passed { Text(d.reasonZh).font(.caption).foregroundColor(.orange).multilineTextAlignment(.trailing) }
                }
                Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 2) {
                    GridRow { Text("风险金额").foregroundColor(.secondary); Text("\(Fmt.usd(d.riskUsd)) USDT（余额 × 5%）") }
                    GridRow { Text("对齐后止损").foregroundColor(.secondary); Text(Fmt.num(d.stop)) }
                    GridRow { Text("自动数量").foregroundColor(.secondary); Text(Fmt.num(d.quantity)) }
                    GridRow { Text("名义价值").foregroundColor(.secondary); Text("\(Fmt.usd(d.notional)) USDT") }
                    GridRow { Text("杠杆/模式").foregroundColor(.secondary); Text("\(d.leverage)x 全仓（下单前自动设置）") }
                }.font(.caption.monospacedDigit())
            } else {
                Text("等待行情…").font(.caption).foregroundColor(.secondary)
            }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 8).stroke(Color.gray.opacity(0.3)))
    }
}

struct WatchlistCard: View {
    @EnvironmentObject var model: AppModel
    @State private var newSymbol = ""
    @State private var adding = false

    var body: some View {
        Card(title: "自选交易对（只有自选里的才能开单）") {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack {
                    ForEach(model.watchlist, id: \.self) { s in
                        Button { model.selectSymbol(s) } label: {
                            Text(s).font(.callout.monospaced())
                                .padding(.horizontal, 10).padding(.vertical, 6)
                                .background(Capsule().fill(s == model.symbol ? Color.accentColor : Color.gray.opacity(0.2)))
                                .foregroundColor(s == model.symbol ? .white : .primary)
                        }
                        .buttonStyle(.plain)
                        .contextMenu {
                            Button(role: .destructive) { model.removeFromWatchlist(s) } label: { Label("移出自选", systemImage: "trash") }
                        }
                    }
                }
            }
            HStack {
                TextField("例如 DOGEUSDT", text: $newSymbol)
                    .textFieldStyle(.roundedBorder)
                    #if os(iOS)
                    .textInputAutocapitalization(.characters)
                    .autocorrectionDisabled()
                    #endif
                Button(adding ? "校验中…" : "加入自选") {
                    let s = newSymbol
                    adding = true
                    Task { await model.addToWatchlist(s); adding = false; newSymbol = "" }
                }.disabled(newSymbol.trimmingCharacters(in: .whitespaces).isEmpty || adding)
            }
            Text("长按（Mac 上右键）交易对可移出自选。加入时会用币安公开接口校验是否为 U 本位永续。").font(.caption2).foregroundColor(.secondary)
        }
    }
}

struct PositionsCard: View {
    @EnvironmentObject var model: AppModel
    var body: some View {
        Card(title: "持仓与止损状态") {
            if model.positions.isEmpty {
                Text("暂无持仓").font(.caption).foregroundColor(.secondary)
            }
            ForEach(model.positions) { p in
                VStack(alignment: .leading, spacing: 2) {
                    HStack {
                        Text("\(p.symbol) \(p.side.zh)").font(.subheadline.weight(.semibold))
                        Spacer()
                        Pill(text: p.statusZh, color: p.protected ? .green : .red)
                    }
                    Text("数量 \(Fmt.num(p.quantity)) · 开仓 \(Fmt.num(p.entry)) · 标记 \(Fmt.num(p.mark))").font(.caption.monospacedDigit())
                    HStack {
                        Text("止损 \(Fmt.num(p.stop))").font(.caption.monospacedDigit())
                        Spacer()
                        Text("未实现 \(Fmt.pnl(p.unrealizedPnl))").font(.caption.monospacedDigit())
                            .foregroundColor(p.unrealizedPnl >= 0 ? .green : .red)
                    }
                }
                Divider()
            }
            if model.mode == .mock {
                Text("模拟模式：标记价格穿过止损时按该价格自动平仓记账（每 5 秒检查一次，App 在前台时）。").font(.caption2).foregroundColor(.secondary)
            }
        }
    }
}

struct LogCard: View {
    @EnvironmentObject var model: AppModel
    var body: some View {
        Card(title: "操作日志（最近 50 条，北京时间）") {
            if model.logs.isEmpty { Text("暂无记录").font(.caption).foregroundColor(.secondary) }
            ForEach(model.logs) { e in
                VStack(alignment: .leading, spacing: 1) {
                    Text("\(e.timeLabel) [\(TradingMode(rawValue: e.mode)?.zh ?? e.mode)] \(e.event) \(e.symbol ?? "") \(e.side ?? "")")
                        .font(.caption.monospaced())
                    Text(e.reasonZh).font(.caption2).foregroundColor(.secondary)
                }
            }
        }
    }
}
