# kaicang

币安 U 本位永续的开仓控制器。风控写死在代码里：全仓、20 倍杠杆、单笔风险为账户余额的 5%，数量按止损距离计算并按 `stepSize` 向下取整。能做的交易操作只有三件：填止损、选方向、开单。

仓库里有两套独立实现，规则对齐，互不依赖。

## `web/`

Python FastAPI 加静态网页。默认模拟模式，不带密钥。可用 Docker 跑，也可以按 `web/render.yaml` 部署到 Render 免费实例（法兰克福）。Blueprint 和 Dockerfile 都在 `web/` 里，部署时把服务根目录设为 `web/`。

```bash
cd web
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pytest
```

说明见 `web/README.md`。

## `app/`

SwiftUI 多平台 App（macOS 与 iOS 共用 `app/App/Shared`）。核心是 Swift 包 `app/ControllerCore`（风控、指标、MACD、签名客户端、钥匙串），界面只负责展示和把操作交给核心。`app/OpenController.xcodeproj` 由 XcodeGen 根据 `app/project.yml` 生成。

```bash
cd app/ControllerCore && swift test
```

Xcode 里打开 `app/OpenController.xcodeproj`，scheme 选 `OpenController-macOS` 或 `OpenController-iOS`。说明见 `app/README.md`，包测试记录见 `app/VERIFICATION.md`。
