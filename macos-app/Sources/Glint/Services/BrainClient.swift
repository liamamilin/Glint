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
        do {
            return try await routeOnce(inputText: inputText, appName: appName)
        } catch {
            // Silent single retry (200ms) absorbs launchd startup / brief loopback hiccups.
            try? await Task.sleep(nanoseconds: 200_000_000)
            return try await routeOnce(inputText: inputText, appName: appName)
        }
    }

    private func routeOnce(inputText: String, appName: String) async throws -> RouteResponse {
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

    func warmup() async {
        let url = baseURL.appendingPathComponent("warmup")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 15.0
        _ = try? await session.data(for: request)
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

    func drawNextSeed(mode: SeedPoolMode, excludeIds: [String] = []) async throws -> SeedDrawResponse {
        let url = baseURL.appendingPathComponent("seed/next")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(
            SeedDrawRequest(mode: mode.rawValue, exclude_ids: excludeIds)
        )
        let (data, response) = try await session.data(for: request)
        try validateHTTP(response)
        return try JSONDecoder().decode(SeedDrawResponse.self, from: data)
    }

    func sendSeedFeedback(
        id: String,
        action: String,
        snoozeDays: Int = 7,
        category: String? = nil
    ) async -> SeedFeedbackResponse? {
        let url = baseURL.appendingPathComponent("seed/feedback")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        do {
            request.httpBody = try JSONEncoder().encode(
                SeedFeedbackRequest(id: id, action: action, snoozeDays: snoozeDays, category: category)
            )
            let (data, response) = try await session.data(for: request)
            try validateHTTP(response)
            return try JSONDecoder().decode(SeedFeedbackResponse.self, from: data)
        } catch {
            NSLog("[Glint] Seed feedback post failed: \(error.localizedDescription)")
            return nil
        }
    }

    func resetSeedState() async -> SeedFeedbackResponse? {
        let url = baseURL.appendingPathComponent("seed/reset")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        do {
            let (data, response) = try await session.data(for: request)
            try validateHTTP(response)
            return try JSONDecoder().decode(SeedFeedbackResponse.self, from: data)
        } catch {
            NSLog("[Glint] Seed reset failed: \(error.localizedDescription)")
            return nil
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
