import SwiftUI
import ControllerCore

enum Fmt {
    static func usd(_ d: Decimal?) -> String {
        guard let d else { return "—" }
        return Dec.str(Dec.roundHalfEven(d, scale: 2))
    }
    static func num(_ d: Decimal?) -> String { d.map { Dec.str($0) } ?? "—" }
    static func pnl(_ d: Decimal) -> String {
        let v = Dec.str(Dec.roundHalfEven(d, scale: 2))
        return d > 0 ? "+" + v : v
    }
}

extension MACD.Tone {
    var color: Color {
        switch self { case .green: return .green; case .red: return .red; case .gray: return .gray }
    }
}

struct Card<Content: View>: View {
    let title: String
    let content: Content

    init(title: String, @ViewBuilder content: () -> Content) {
        self.title = title
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title).font(.headline)
            content
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12).fill(Color.gray.opacity(0.12)))
    }
}

struct Pill: View {
    let text: String
    let color: Color
    /// 灰色中性样式。最低系统是 macOS 13 / iOS 16，而 Color 的 Equatable 从 macOS 14 / iOS 17 才有。
    var neutral: Bool = false
    var body: some View {
        Text(text)
            .font(.callout.weight(.semibold))
            .foregroundColor(neutral ? .secondary : .white)
            .padding(.horizontal, 10).padding(.vertical, 6)
            .background(Capsule().fill(neutral ? Color.gray.opacity(0.2) : color))
    }
}
