import SwiftUI
import ControllerCore

struct SettingsView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var keyMode: TradingMode = .testnet
    @State private var apiKey = ""
    @State private var secret = ""
    @State private var message = ""
    @State private var confirmText = ""
    @State private var showReset = false

    var modeBinding: Binding<TradingMode> {
        Binding(get: { model.mode }, set: { model.setMode($0) })
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("运行模式") {
                    Picker("模式", selection: modeBinding) {
                        ForEach(TradingMode.allCases) { Text($0.zh).tag($0) }
                    }.pickerStyle(.segmented)
                    Text("默认模拟。测试网用币安 Demo Trading 的 Key；实盘必须在下方二次确认，且只在本次运行有效，重启 App 会回到模拟。")
                        .font(.caption).foregroundColor(.secondary)
                }

                if model.mode == .live {
                    Section("实盘二次确认") {
                        if model.liveArmed {
                            Label("实盘已开启（本次运行）", systemImage: "exclamationmark.triangle.fill").foregroundColor(.red)
                            Button("关闭实盘下单", role: .destructive) { model.disarmLive() }
                        } else {
                            Text("真实资金。请输入「确认实盘」后点开启。").font(.caption)
                            TextField("确认实盘", text: $confirmText)
                            Button("开启实盘下单") {
                                message = model.armLive(confirmText: confirmText) ?? "已开启实盘"
                                confirmText = ""
                            }.tint(.red)
                        }
                    }
                }

                Section("填写自己的币安 API（存本机钥匙串）") {
                    Picker("用于", selection: $keyMode) {
                        Text("测试网").tag(TradingMode.testnet)
                        Text("实盘").tag(TradingMode.live)
                    }.pickerStyle(.segmented)
                    if let masked = model.maskedKeys[keyMode] {
                        Text("已保存：Key \(masked)（Secret 不显示）").font(.caption)
                    }
                    TextField("API Key", text: $apiKey)
                        #if os(iOS)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        #endif
                    SecureField("Secret Key", text: $secret)
                    HStack {
                        Button("保存") {
                            message = model.saveKeys(apiKey: apiKey, secret: secret, for: keyMode)
                            apiKey = ""; secret = ""
                        }
                        Spacer()
                        Button("只读验证") { Task { message = await model.verifyKeys(for: keyMode) } }
                        Spacer()
                        Button("删除", role: .destructive) { model.deleteKeys(for: keyMode); message = "已删除" }
                    }
                    Text("建议：只勾选「允许合约」，不要勾选提现；绑定你自己的出口 IP。请求从这台设备直接发往币安，不经过任何服务器。")
                        .font(.caption).foregroundColor(.secondary)
                }

                if !message.isEmpty {
                    Section { Text(message).font(.callout) }
                }

                if model.mode == .mock {
                    Section("模拟账户") {
                        Button("重置模拟账户为 1000 USDT", role: .destructive) { showReset = true }
                    }
                }

                Section("写死的规则（不可修改）") {
                    Text("U 本位永续 · 全仓 · \(RiskRules.leverage) 倍 · 单笔风险 = 余额 × 5% · 数量 = 风险 ÷ |标记价 − 止损| 向下取整 · 止损必填且在正确一侧 · 只能开自选 · 市价开仓 + reduce-only 止损（按标记价格触发），止损挂失败立即平仓")
                        .font(.caption)
                    Text("指标：\(IndicatorParams.maLabel)、Supertrend(\(IndicatorParams.supertrendAtrPeriod),\(Dec.str(IndicatorParams.supertrendMultiplier)))、\(MACDParams.label)，全部用币安官方 K 线在本机计算，只用已收盘 K 线。")
                        .font(.caption)
                }
            }
            .navigationTitle("设置")
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } } }
            .alert("重置模拟账户？", isPresented: $showReset) {
                Button("重置", role: .destructive) { model.resetMock() }
                Button("取消", role: .cancel) {}
            }
        }
        #if os(macOS)
        .frame(minWidth: 480, minHeight: 600)
        #endif
    }
}
