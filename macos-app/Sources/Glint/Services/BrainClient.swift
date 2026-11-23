import Foundation

/// HTTP client talking to the local Python FastAPI brain-server (localhost:8765).
final class BrainClient: Sendable {
    let baseURL: URL
    private let session: URLSession

    init(baseURL: URL = URL(string: "http://127.0.0.1:18765")!) {
        self.baseURL = baseURL
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 6.0
        config.timeoutIntervalForResource = 10.0
        self.session = URLSession(configuration: config)
    }

    func health() async throws -> HealthResponse {
        let url = baseURL.appendingPathComponent("health")
        let (data, response) = try await session.data(from: url)
        try validateHTTP(response)
        return try JSONDecoder().decode(HealthResponse.self, from: data)
    }

    func route(inputText: String, appName: String) async throws -> RouteResponse {
        let url = baseURL.appendingPathComponent("route")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(
            RouteRequest(inputText: inputText, appName: appName)
        )

        let (data, response) = try await session.data(for: request)
        try validateHTTP(response)
        return try JSONDecoder().decode(RouteResponse.self, from: data)
    }

    func sendFeedback(_ payload: FeedbackRequest) async {
        let url = baseURL.appendingPathComponent("feedback")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        do {
            request.httpBody = try JSONEncoder().encode(payload)
            let (_, response) = try await session.data(for: request)
            try validateHTTP(response)
        } catch {
            // Feedback logging is best-effort and must never interrupt the user flow.
            NSLog("[Glint] Feedback post failed: \(error.localizedDescription)")
        }
    }

    private func validateHTTP(_ response: URLResponse) throws {
        guard let http = response as? HTTPURLResponse else {
            throw URLError(.badServerResponse)
        }
        guard (200...299).contains(http.statusCode) else {
            throw NSError(
                domain: "Glint.BrainClient",
                code: http.statusCode,
                userInfo: [NSLocalizedDescriptionKey: "HTTP \(http.statusCode)"]
            )
        }
    }
}
