# 验证记录（2026-11-10，Asia/Shanghai）

环境：Linux x86_64，Swift 6.4.2（swiftly 安装）。Linux 上没有 SwiftUI / CryptoKit / 钥匙串，所以下面只覆盖 `ControllerCore` 包。

## 1. 单元测试：`cd ControllerCore && swift test`

44 个测试全部通过，覆盖：
- 风控引擎：5% 风险、向下取整、minQty / minNotional、止损必填 / 无效 / 方向错误、tick 对齐方向、自选门槛、规则常量
- 开单流程（模拟账本 + 可控桩）：先设全仓 20x 再开仓再挂止损；设置失败不开仓；发送前 mark 变化导致止损到了错误一侧时拒绝且不发任何请求；止损失败 → 平仓；止损和平仓都失败 → 标「未受保护」；实盘未二次确认时拒绝；模拟止损触发记账；模拟保证金不足
- 签名客户端（假网络）：测试网地址 `demo-fapi.binance.com`；止损走 `POST /fapi/v1/algoOrder`，带 `algoType=CONDITIONAL / type=STOP_MARKET / reduceOnly=true / workingType=MARK_PRICE / triggerPrice`；开仓 MARKET 不带 reduceOnly，平仓带 reduceOnly；签名覆盖完整 query；`-4046` 已是全仓视为成功，`-4048` 等其它错误拒绝；杠杆返回值不是 20 时拒绝；双向持仓拒绝；错误信息不含签名或 Secret；持仓和 Algo 止损单配对
- 自选校验：永续可加，交割合约拒绝，接口报错拒绝
- HMAC-SHA256：币安官方示例签名、超长 Key、空消息、长消息

## 2. 与 Python 版对拍

`Tools/parity_python.py` 用原 Python 代码（`/workspace/binance-open-controller/app`）生成输入和期望输出，`parity-cli fixture` 用同一份输入算 Swift 结果，`Tools/compare.py` 比对。

| 类别 | 用例数 |
|---|---|
| 风控 evaluate | 1101（PASS 327；QTY_BELOW_MIN 198、INVALID_INPUT 251、INVALID_SIDE 142、INVALID_STOP 86、MISSING_STOP 60、方向错误 37） |
| 止损自动填入 suggest_stop | 300（可填 146 / 不可填 154） |
| K 线序列（SMA20、Supertrend(10,3)、MACD 交叉、小级别背驰、K 线标签） | 57：16 组**真实**币安 K 线（BTC / ETH / DOGE / 1000PEPE × 15m/1h/4h/1d，加对应小级别），40 组合成序列（5 种价格量级、5~200 根），1 组含未收盘 K 线 |
| HMAC 签名 | 41 |

结果：**12,503 个字段，0 个不一致**。其中 11,941 个逐字相同；562 个（`raw_quantity`、`sma`、`supertrend`）在 10⁻²⁰ 相对误差内相同，差别来自 Python Decimal 默认 28 位有效数字、Swift Decimal 38 位。下单用到的数量、止损、名义价值、原因码、中文原因、MACD 的 DIF/DEA（8 位小数）全部逐字一致。

## 3. 真实数据（公开接口，不需要 Key，不下单）

`ControllerCore/.build/release/parity-cli live BTCUSDT`，22:22（Asia/Shanghai），Swift 用 URLSession 直接请求 `fapi.binance.com`：

```
symbol=BTCUSDT mark=73822 tick=0.1 step=0.001 minQty=0.001 minNotional=50
[1h] SMA20=74713.205 | ST(10,3)=75493.66102258… | 1h MACD：死叉（8根前） | 15m 小级别：无背驰
[4h] SMA20=75314.7   | ST(10,3)=76872.48743003… | 4h MACD：金叉（3根前） | 1h 小级别：无背驰
[1日] SMA20=78675.14 | ST(10,3)=81637.19923997… | 1d MACD：死叉（41根前）| 4h 小级别：无背驰
示例风控(余额1000, 多, 止损=mark×0.99): PASS qty=0.067 stop=73083.7 risk=50
```

同一时间用 Python 版计算 1h / 4h / 1d：SMA、Supertrend、MACD 标签和 DIF 完全一致。

另外确认 `demo-fapi.binance.com` 的 premiumIndex / exchangeInfo / time 可以访问；主网和测试网的 `/fapi/v1/algoOrder`、`/fapi/v1/openAlgoOrders` 存在（用无效 Key 调用时返回 -2014 鉴权错误，而不是 404）。

## 4. 未验证

- **SwiftUI 界面、macOS / iOS 编译、CryptoKit 签名路径、钥匙串读写都没有编译或运行过**（Linux 上没有这些框架）。`AppModel.swift` 用 Combine 替身在 Linux 上做过类型检查；界面代码没有编译过。第一次在 Xcode 里编译如果报错，多半是小的 API 差异。
- 没有用真实 Key 在测试网或实盘下过单。Algo 止损单接口的参数来自币安官方 SDK 和文档，没有用真实账户验证。
