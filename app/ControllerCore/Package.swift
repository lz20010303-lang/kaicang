// swift-tools-version:5.9
// 核心包：风控引擎、指标、币安客户端。与 UI 无关，可在 Linux 上编译和测试。
// 没有任何第三方依赖：Apple 平台用系统 CryptoKit 签名，Xcode 打开时不需要联网拉包。
import PackageDescription

let package = Package(
    name: "ControllerCore",
    platforms: [.macOS(.v13), .iOS(.v16)],
    products: [
        .library(name: "ControllerCore", targets: ["ControllerCore"]),
    ],
    targets: [
        .target(name: "ControllerCore"),
        // 与 Python 版对拍、真实 K 线测试用的命令行工具（不进 App）。
        .executableTarget(name: "parity-cli", dependencies: ["ControllerCore"]),
        .testTarget(name: "ControllerCoreTests", dependencies: ["ControllerCore"]),
    ]
)
