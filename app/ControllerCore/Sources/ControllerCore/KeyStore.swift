import Foundation
#if canImport(Security)
import Security
#endif

public struct APICredentials: Equatable, Sendable {
    public var apiKey: String
    public var secret: String
    public init(apiKey: String, secret: String) { self.apiKey = apiKey; self.secret = secret }
    /// 界面上只显示 Key 尾号，Secret 永不显示。
    public var maskedKey: String { apiKey.count <= 4 ? "****" : "****" + apiKey.suffix(4) }
}

public protocol CredentialStore: AnyObject {
    func load(mode: TradingMode) -> APICredentials?
    func save(_ c: APICredentials, mode: TradingMode) throws
    func delete(mode: TradingMode) throws
}

public final class InMemoryCredentialStore: CredentialStore {
    var items: [TradingMode: APICredentials] = [:]
    public init() {}
    public func load(mode: TradingMode) -> APICredentials? { items[mode] }
    public func save(_ c: APICredentials, mode: TradingMode) throws { items[mode] = c }
    public func delete(mode: TradingMode) throws { items[mode] = nil }
}

#if canImport(Security)
/// Key/Secret 存在系统钥匙串（Keychain），仅本设备、解锁后可读，不同步 iCloud。
public final class KeychainCredentialStore: CredentialStore {
    let service: String
    public init(service: String = "OpenController.binance") { self.service = service }

    func query(_ account: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: account]
    }

    func read(_ account: String) -> String? {
        var q = query(account)
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: AnyObject?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let d = out as? Data else { return nil }
        return String(data: d, encoding: .utf8)
    }

    func write(_ account: String, _ value: String) throws {
        SecItemDelete(query(account) as CFDictionary)
        var q = query(account)
        q[kSecValueData as String] = Data(value.utf8)
        q[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
        let st = SecItemAdd(q as CFDictionary, nil)
        guard st == errSecSuccess else { throw ClientError("保存到钥匙串失败（\(st)）", code: "KEYCHAIN") }
    }

    public func load(mode: TradingMode) -> APICredentials? {
        guard let k = read("\(mode.rawValue).key"), let s = read("\(mode.rawValue).secret"), !k.isEmpty, !s.isEmpty else { return nil }
        return APICredentials(apiKey: k, secret: s)
    }
    public func save(_ c: APICredentials, mode: TradingMode) throws {
        try write("\(mode.rawValue).key", c.apiKey.trimmingCharacters(in: .whitespacesAndNewlines))
        try write("\(mode.rawValue).secret", c.secret.trimmingCharacters(in: .whitespacesAndNewlines))
    }
    public func delete(mode: TradingMode) throws {
        SecItemDelete(query("\(mode.rawValue).key") as CFDictionary)
        SecItemDelete(query("\(mode.rawValue).secret") as CFDictionary)
    }
}
#endif
