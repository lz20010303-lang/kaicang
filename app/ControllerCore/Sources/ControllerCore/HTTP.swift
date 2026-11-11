import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

public struct HTTPResponse: Sendable { public let status: Int; public let body: Data }

/// 网络层抽象，方便测试时替换。App 内用 URLSession 从本机直连币安。
public protocol HTTPTransport: Sendable {
    func send(method: String, url: URL, headers: [String: String]) async throws -> HTTPResponse
}

public struct URLSessionTransport: HTTPTransport {
    public let timeout: TimeInterval
    public init(timeout: TimeInterval = 10) { self.timeout = timeout }

    public func send(method: String, url: URL, headers: [String: String]) async throws -> HTTPResponse {
        var req = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: timeout)
        req.httpMethod = method
        for (k, v) in headers { req.setValue(v, forHTTPHeaderField: k) }
        let (data, resp) = try await withCheckedThrowingContinuation { (cont: CheckedContinuation<(Data, URLResponse), Error>) in
            URLSession.shared.dataTask(with: req) { data, resp, err in
                if let err { cont.resume(throwing: err) }
                else if let resp { cont.resume(returning: (data ?? Data(), resp)) }
                else { cont.resume(throwing: URLError(.badServerResponse)) }
            }.resume()
        }
        let status = (resp as? HTTPURLResponse)?.statusCode ?? 0
        return HTTPResponse(status: status, body: data)
    }
}

public struct ClientError: Error, Sendable, CustomStringConvertible {
    public let message: String
    public let code: String
    public let httpStatus: Int?
    public init(_ message: String, code: String, httpStatus: Int? = nil) {
        self.message = message; self.code = code; self.httpStatus = httpStatus
    }
    public var description: String { message }
}

enum JSON {
    static func parse(_ data: Data) throws -> Any {
        try JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed])
    }
}
