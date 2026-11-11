# 开仓控制器（Mac + iPhone 本地 App）

币安 U 本位永续合约的开仓工具。风控规则写死在代码里，App 里没有任何地方能改。
每个人在自己的设备上安装，填自己的 API。请求从这台设备直接发往币安，Key 只存在本机钥匙串里，不经过任何服务器。全程免费，不需要 99 美元的开发者账号。

## 一、写死的规则

| 规则 | 实现 |
|---|---|
| 品种 | 只做 U 本位永续（USDT 计价、PERPETUAL、可交易） |
| 杠杆 / 保证金 | 每次开单前自动设为 **全仓 + 20 倍**，失败就不开单；双向持仓模式直接拒绝（只支持单向持仓） |
| 以损定仓 | 风险 = 余额 × 5%；数量 = 风险 ÷ \|标记价 − 止损\|，按 stepSize **向下**取整；不足 minQty / minNotional 直接拒绝，绝不向上凑 |
| 止损 | 必填，做多必须低于标记价，做空必须高于；按 tick 对齐时只会放宽不会收紧（多向下、空向上） |
| 自选 | 只能对自选里的交易对开单；加自选时用币安公开接口校验是否为 U 本位永续 |
| 发送前复核 | 点「开单」后先取**最新**标记价、余额、过滤器，把全部规则重跑一遍，任何一条不过都不发单 |
| 下单方式 | 市价开仓 → reduce-only STOP_MARKET（按标记价格 MARK_PRICE 触发，走币安 2025-12-09 起要求的 Algo 条件单接口 `/fapi/v1/algoOrder`）→ 止损挂失败立即市价平掉新仓；平仓也失败就红字提示「未受保护，请立即人工处理」 |
| 模式 | 模拟（默认，本地账本，余额 1000 USDT）/ 测试网 / 实盘。实盘必须在设置里输入「确认实盘」二次确认，只在本次运行有效，重启 App 回到模拟 |

**你能做的只有三件事：① 填止损 ② 选方向 ③ 开单。** 另外可以管理自选、切换周期、选止损来源、看持仓与日志。

辅助功能（都只读币安官方主网公开 K 线、在本机计算、只用已收盘 K 线）：
- 周期：15min / 1h / 4h / 1日
- 止损来源：手动 / 均线 SMA20 / Supertrend(10, 3)。选中后按当前周期自动填入：做多只填价格下方的线，做空只填上方的线，没选方向时只填下方的线；条件不满足就留空并写明原因
- MACD(12,26,9) 金叉 / 死叉，以及小一级周期（1日→4h、4h→1h、1h→15m、15m→5m）的顶 / 底背驰。和开单方向相同标绿，相反标红，没选方向或没有信号是灰色。**仅供参考，不影响开单**

这些算法和原来 Python 网页版（`app/engine.py`、`app/indicators.py`、`app/macd.py`）一条一条对过，见 `VERIFICATION.md`。

## 二、工程结构

```
app/
├── OpenController.xcodeproj     ← 双击这个打开（已生成好）
├── project.yml                  ← XcodeGen 描述（改了才需要重新生成）
├── Config/Signing.xcconfig      ← Bundle ID 前缀 / Team（一般不用改）
├── App/
│   ├── Shared/                  ← SwiftUI 界面，Mac 和 iPhone 共用一套代码
│   └── macOS/OpenController.entitlements（沙盒 + 允许联网）
├── ControllerCore/              ← 与界面无关的 Swift Package（可在 Linux 编译测试）
│   ├── Sources/ControllerCore/
│   │   ├── Engine.swift         风控引擎（写死的规则）
│   │   ├── Indicators.swift     SMA / Supertrend / 自动填止损
│   │   ├── MACD.swift           金叉死叉 / 背驰
│   │   ├── Controller.swift     开单流程（复核 → 全仓20x → 开仓 → 止损 → 失败平仓）
│   │   ├── BinanceClient.swift  测试网 / 实盘签名客户端（HMAC-SHA256）
│   │   ├── MockExchange.swift   模拟账本（存在 App 自己的目录里）
│   │   ├── Signing.swift        签名（Apple 上用 CryptoKit）
│   │   ├── Stores.swift         自选、操作日志
│   │   └── KeyStore.swift       钥匙串存 Key / Secret
│   ├── Sources/parity-cli/      对拍 / 真实数据测试工具（不进 App）
│   └── Tests/                   单元测试
└── Tools/                       与 Python 版对拍的脚本
```

没有任何第三方依赖，Xcode 打开时不需要联网拉包。

## 三、在 Mac 上运行（免费）

1. **装 Xcode**：Mac App Store 搜 Xcode，免费。装最新版（iPhone 16 Pro Max 的系统比较新，旧 Xcode 认不出来）。装完打开一次，让它装完组件。
2. **登录 Apple ID**：Xcode → Settings（设置）→ Accounts → 左下角 + → Apple ID，用你自己的普通 Apple ID 登录，会出现一个「(你的名字) (Personal Team)」。
3. **打开工程**：解压后双击 `OpenController.xcodeproj`。
4. **选签名**：左侧点最上面的 OpenController 工程 → TARGETS 里分别选 `OpenController-macOS` 和 `OpenController-iOS` → Signing & Capabilities → 勾上 Automatically manage signing → Team 选你的 Personal Team。两个 target 各选一次。
   - 如果提示 Bundle Identifier 无法注册（被别人占用了），把 `Config/Signing.xcconfig` 里的 `OC_BUNDLE_PREFIX` 改成 `com.你的拼音.opencontroller` 这类独一无二的名字，或者在同一页面直接改 Bundle Identifier。
5. **运行 Mac 版**：Xcode 顶部中间的 scheme 选 `OpenController-macOS`，目标选 My Mac，按 ⌘R。
   - 想像普通软件一样用：Product → Show Build Folder in Finder，在 `Products/Debug/` 里把 `OpenController.app` 拖到「应用程序」。
   - 本机自己编译、自己签名的 App 不需要公证。如果哪天打不开了，回 Xcode 再按一次 ⌘R 就行。

## 四、装到 iPhone（免费个人签名）

1. 用数据线把 iPhone 接到 Mac，iPhone 上点「信任此电脑」并输入锁屏密码。
2. **打开开发者模式**（iOS 16 及以上需要）：iPhone 设置 → 隐私与安全性 → 最底部「开发者模式」→ 打开 → 按提示重启 → 重启后再确认一次「打开」。如果看不到这个选项，先在 Xcode 里选一次你的 iPhone 作为运行目标，它就会出现。
3. Xcode 顶部 scheme 选 `OpenController-iOS`，目标选你的 iPhone（第一次连接 Xcode 会花几分钟准备设备），按 ⌘R。
4. 第一次装好后，iPhone 上会提示「不受信任的开发者」：设置 → 通用 → VPN 与设备管理 → 开发者 App 下点你的 Apple ID → 信任。然后再打开 App。
5. 以后可以不插线：Xcode → Window → Devices and Simulators → 选中 iPhone → 勾「Connect via network」，同一 Wi-Fi 下就能无线安装。

### 免费签名的限制（Apple 官方说明）
- 免费 Apple ID（Personal Team）签的 App **7 天后过期**，过期后图标还在但点不开。
- 每台设备最多同时装 3 个这种 App；最多注册 3 台设备；7 天内最多 10 个 App ID。
- 过期后怎么办：插上 iPhone（或无线连接），在 Xcode 里选 `OpenController-iOS` 和你的 iPhone，再按一次 ⌘R 就行。**不要先删 App**，直接覆盖安装，自选、日志、模拟账本和钥匙串里的 Key 都会保留。
- 如果提示描述文件 / 证书问题：Xcode → Settings → Accounts → 选你的 Apple ID → Download Manual Profiles，或在 Signing 页面把 Team 重新选一次。

## 五、币安 API 设置建议

1. 先用**测试网**：登录 <https://demo.binance.com> 的合约 Demo Trading，在 API Management 里创建 Key。App 里设置 → 模式选「测试网」→ API 用于「测试网」→ 填 Key 和 Secret → 保存 → 点「只读验证」。
2. 实盘 Key 在 binance.com → API 管理里创建：
   - **只勾「允许合约」**（读取默认开）。**不要勾「允许提现」**，也不需要现货 / 杠杆 / 万向划转。
   - **绑定 IP 白名单**，填你这台设备上网的出口 IP（开代理的话就是代理节点的出口 IP）。出口 IP 变了下单会报 -2015，需要去币安改白名单。
   - 合约账户要是**单向持仓**模式（币安合约 → 偏好设置 → 持仓模式），双向持仓 App 会拒绝开单。
3. Key 存在系统钥匙串里，只在这台设备上、解锁后可读，不会同步到 iCloud；界面只显示 Key 尾号，Secret 永远不显示。设置里可以随时删除。
4. 实盘开关：模式选「实盘」→ 输入「确认实盘」→ 开启。每次重新打开 App 都要重新确认一次。

## 六、中国大陆网络

- 大陆网络一般连不上币安 API（`fapi.binance.com`），需要你自己开代理 / VPN，而且要让这个 App 的流量也走代理（iPhone 上用全局模式最省事；Mac 上用系统代理或 TUN 模式）。
- 代理节点不要选币安限制服务的地区（比如美国），否则会返回 451 / 403。
- 绑了 IP 白名单的话，白名单要填**代理节点的出口 IP**。
- 页面顶部或持仓区出现「连接币安失败」，先检查代理。

## 七、已知限制

- 仓位按止损距离算，**没有计入手续费和滑点**，实际亏损可能略大于 5%；止损按标记价格触发，成交价可能比止损价更差（跳空、插针时更明显）。
- 指标有滞后，只用已收盘 K 线，和 TradingView 的数值可能有细微差别（例如对方用 EMA、或包含正在走的那根 K 线）。Supertrend 和 MACD 依赖历史长度，这里固定取最近 200 根（MACD 取 200 根，背驰回看 100 根）。
- 测试网的价格和主网不同步：指标用的是主网 K 线，测试网开单用测试网标记价格，所以测试网里自动填入的止损有时会在价格错误的一侧，App 会拒绝开单（规则如此，不是 bug）。
- iPhone 上 App 切到后台会被系统暂停：**实盘 / 测试网的止损单挂在币安服务器上，不受影响**；模拟模式的止损检查只在 App 前台时进行。
- 不会自动撤单：如果你在币安上手动平了仓，之前挂的 reduce-only 止损单可能还在，请到币安里撤掉，避免影响之后同方向的新仓。
- 同一交易对已有持仓时再开单，新止损只覆盖新开的数量。
- 有些币的最大杠杆低于 20 倍，设杠杆会失败，App 会直接拒绝开单。
- 免费签名 7 天过期，需要按上面的方法重装。
