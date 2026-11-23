import CoreGraphics
import Foundation

enum InjectMode: String, Codable, Sendable {
    case append
    case wrap
}

struct CardItem: Codable, Identifiable, Equatable, Sendable {
    let id: String
    let title: String
    let dimension: String
    let intent: String
    let mode: InjectMode
    let prompt: String
    let preview: String
}

struct RouteRequest: Codable, Sendable {
    let input_text: String
    let app_name: String
    let skill_hint: String?

    init(inputText: String, appName: String, skillHint: String? = nil) {
        self.input_text = inputText
        self.app_name = appName
        self.skill_hint = skillHint
    }
}

struct RouteResponse: Codable, Sendable {
    let skill_type: String
    let cards: [CardItem]
    let latency_ms: Double
    let model: String
    let used_fallback: Bool
}

struct FeedbackRequest: Codable, Sendable {
    let app_name: String
    let skill_type: String
    let input_text: String
    let recommended_ids: [String]
    let selected_id: String?
    let inject_mode: String?
    let latency_ms: Double?
}

struct HealthResponse: Codable, Sendable {
    let status: String
    let model: String
    let cards_loaded: Int
    let ollama_reachable: Bool
}

enum CaptureScope: String, Sendable {
    case selection
    case paragraph
    case fullField
}

struct CapturedContext: Sendable {
    let appName: String
    let appPID: pid_t
    let inputText: String
    let captureScope: CaptureScope
    let anchorRect: CGRect?
}
