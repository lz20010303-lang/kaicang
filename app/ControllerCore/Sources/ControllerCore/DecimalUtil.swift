import Foundation

/// 十进制工具。全部用 Foundation.Decimal，不用 Double，避免二进制浮点误差。
public enum Dec {
    private static let pattern = try! NSRegularExpression(pattern: "^[+-]?([0-9]+\\.?[0-9]*|\\.[0-9]+)([eE][+-]?[0-9]+)?$")

    /// 严格解析：只接受完整的数字字符串，"1abc"、"NaN"、"inf" 一律返回 nil。
    public static func parse(_ text: String?) -> Decimal? {
        guard let raw = text?.trimmingCharacters(in: .whitespacesAndNewlines), !raw.isEmpty else { return nil }
        let range = NSRange(raw.startIndex..<raw.endIndex, in: raw)
        guard pattern.firstMatch(in: raw, options: [], range: range) != nil else { return nil }
        guard let value = Decimal(string: raw, locale: Locale(identifier: "en_US_POSIX")) else { return nil }
        return value.isFinite ? value : nil
    }

    /// JSON 里的数字可能是字符串或 NSNumber。
    public static func from(_ any: Any?) -> Decimal? {
        switch any {
        case let s as String: return parse(s)
        case let i as Int: return Decimal(i)
        case let d as Decimal: return d.isFinite ? d : nil
        case let n as NSNumber: return parse(n.stringValue)
        default: return nil
        }
    }

    /// 与 Python `_decimal_str` 一致：普通十进制写法，去掉尾随 0 和小数点。
    public static func str(_ value: Decimal) -> String {
        var text = NSDecimalNumber(decimal: value).stringValue
        if text.contains("e") || text.contains("E") {
            text = value.description
        }
        if text.contains(".") {
            while text.hasSuffix("0") { text.removeLast() }
            if text.hasSuffix(".") { text.removeLast() }
        }
        if text.isEmpty || text == "-0" { return "0" }
        return text
    }

    public static func strOpt(_ value: Decimal?) -> String? {
        guard let value else { return nil }
        return str(value)
    }

    /// 向 0 方向取整到整数（输入为正数时即 floor），对应 Python ROUND_DOWN。
    public static func truncInt(_ value: Decimal) -> Decimal {
        var v = value
        var r = Decimal()
        NSDecimalRound(&r, &v, 0, value >= 0 ? .down : .up)
        return r
    }

    /// 远离 0 方向取整到整数（输入为正数时即 ceil），对应 Python ROUND_UP。
    public static func awayInt(_ value: Decimal) -> Decimal {
        let t = truncInt(value)
        if t == value { return t }
        return value >= 0 ? t + 1 : t - 1
    }

    /// 四舍六入五成双（Python Decimal 默认 ROUND_HALF_EVEN），保留 `scale` 位小数。
    public static func roundHalfEven(_ value: Decimal, scale: Int) -> Decimal {
        var v = value
        var r = Decimal()
        NSDecimalRound(&r, &v, scale, .bankers)
        return r
    }
}
