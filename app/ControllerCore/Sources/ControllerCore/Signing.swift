import Foundation
#if canImport(CryptoKit)
import CryptoKit
#endif

/// 币安 HMAC-SHA256 签名。
/// - Apple 平台（App 实际运行）：系统 CryptoKit。
/// - 其他平台（仅用于在 Linux 上跑测试）：下方的纯 Swift SHA-256 实现，已用币安官方示例和 Python hmac 对拍。
public enum Signer {
    public static func hmacSHA256Hex(secret: String, message: String) -> String {
        #if canImport(CryptoKit)
        let key = SymmetricKey(data: Data(secret.utf8))
        let mac = HMAC<SHA256>.authenticationCode(for: Data(message.utf8), using: key)
        return mac.map { String(format: "%02x", $0) }.joined()
        #else
        return PortableHMAC.hmacSHA256(key: Array(secret.utf8), message: Array(message.utf8)).map { String(format: "%02x", $0) }.joined()
        #endif
    }

    /// 与 Python urllib.parse.urlencode(quote_plus) 一致的编码。
    public static func encode(_ value: String) -> String {
        var out = ""
        for scalar in value.unicodeScalars {
            let v = scalar.value
            let unreserved = (v >= 0x30 && v <= 0x39) || (v >= 0x41 && v <= 0x5A) || (v >= 0x61 && v <= 0x7A) || "-_.~".unicodeScalars.contains(scalar)
            if unreserved { out.unicodeScalars.append(scalar) }
            else if scalar == " " { out += "+" }
            else { for b in String(scalar).utf8 { out += String(format: "%%%02X", b) } }
        }
        return out
    }

    public static func queryString(_ params: [(String, String)]) -> String {
        params.map { "\(encode($0.0))=\(encode($0.1))" }.joined(separator: "&")
    }

    /// 生成签名后的 query：参数原样顺序 + timestamp + recvWindow，再附 signature。
    /// 被签名的字符串与实际发送的字符串完全相同。
    public static func signedQuery(_ params: [(String, String)], secret: String, timestampMs: Int64,
                                   recvWindow: Int = 5000) -> String {
        let q = queryString(params + [("timestamp", String(timestampMs)), ("recvWindow", String(recvWindow))])
        return q + "&signature=" + hmacSHA256Hex(secret: secret, message: q)
    }
}

/// FIPS 180-4 SHA-256 + RFC 2104 HMAC，纯 Swift。
enum PortableHMAC {
    static let k: [UInt32] = [
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
        0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
        0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
        0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
        0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
        0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
        0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
    ]

    static func rotr(_ x: UInt32, _ n: UInt32) -> UInt32 { (x >> n) | (x << (32 - n)) }

    static func sha256(_ input: [UInt8]) -> [UInt8] {
        var h: [UInt32] = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]
        var msg = input
        let bitLen = UInt64(input.count) * 8
        msg.append(0x80)
        while msg.count % 64 != 56 { msg.append(0) }
        for i in (0..<8).reversed() { msg.append(UInt8((bitLen >> (UInt64(i) * 8)) & 0xff)) }
        var w = [UInt32](repeating: 0, count: 64)
        for chunk in stride(from: 0, to: msg.count, by: 64) {
            for i in 0..<16 {
                let b = chunk + i * 4
                w[i] = UInt32(msg[b]) << 24 | UInt32(msg[b + 1]) << 16 | UInt32(msg[b + 2]) << 8 | UInt32(msg[b + 3])
            }
            for i in 16..<64 {
                let s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >> 3)
                let s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >> 10)
                w[i] = w[i - 16] &+ s0 &+ w[i - 7] &+ s1
            }
            var a = h[0], b = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], hh = h[7]
            for i in 0..<64 {
                let S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)
                let ch = (e & f) ^ (~e & g)
                let t1 = hh &+ S1 &+ ch &+ k[i] &+ w[i]
                let S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)
                let maj = (a & b) ^ (a & c) ^ (b & c)
                let t2 = S0 &+ maj
                hh = g; g = f; f = e; e = d &+ t1; d = c; c = b; b = a; a = t1 &+ t2
            }
            h[0] = h[0] &+ a; h[1] = h[1] &+ b; h[2] = h[2] &+ c; h[3] = h[3] &+ d
            h[4] = h[4] &+ e; h[5] = h[5] &+ f; h[6] = h[6] &+ g; h[7] = h[7] &+ hh
        }
        return h.flatMap { v in [UInt8(v >> 24), UInt8((v >> 16) & 0xff), UInt8((v >> 8) & 0xff), UInt8(v & 0xff)] }
    }

    static func hmacSHA256(key: [UInt8], message: [UInt8]) -> [UInt8] {
        var k0 = key.count > 64 ? sha256(key) : key
        k0 += [UInt8](repeating: 0, count: 64 - k0.count)
        let ipad = k0.map { $0 ^ 0x36 }, opad = k0.map { $0 ^ 0x5c }
        return sha256(opad + sha256(ipad + message))
    }
}
