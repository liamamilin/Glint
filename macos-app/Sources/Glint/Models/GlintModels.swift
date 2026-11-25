import CoreGraphics
import Foundation

enum InjectMode: String, Codable, Sendable {
    case append
    case wrap
}

struct IntentPrediction: Codable, Equatable, Sendable {
    let primitive: String
    let goal: String
    let clarity: String
    let used_embed: Bool?
}

struct CardItem: Codable, Identifiable, Equatable, Sendable {
    let id: String
    let title: String
    let dimension: String
    let intent: String
    let mode: InjectMode
    let prompt: String
    let preview: String
    let action_type: String?
    let primitive: String?
    let source: String?
    let is_exploration: Bool?
    let workflow_id: String?
    let workflow_step: Int?
    let is_workflow_next: Bool?
    let is_l3_breakthrough: Bool?

    var isReaderAction: Bool {
        action_type == "show_checklist" || action_type == "open_resource"
    }
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
    let intent: IntentPrediction?
    let release_version: String?
}

struct FeedbackRequest: Codable, Sendable {
    let app_name: String
    let skill_type: String
    let input_text: String
    let recommended_ids: [String]
    let selected_id: String?
    let inject_mode: String?
    let latency_ms: Double?
    let custom_intent: String?
    let predicted_primitive: String?
    let predicted_goal: String?
    let predicted_clarity: String?
    let release_version: String?

    init(
        app_name: String,
        skill_type: String,
        input_text: String,
        recommended_ids: [String],
        selected_id: String?,
        inject_mode: String?,
        latency_ms: Double?,
        custom_intent: String? = nil,
        predicted_primitive: String? = nil,
        predicted_goal: String? = nil,
        predicted_clarity: String? = nil,
        release_version: String? = nil
    ) {
        self.app_name = app_name
        self.skill_type = skill_type
        self.input_text = input_text
        self.recommended_ids = recommended_ids
        self.selected_id = selected_id
        self.inject_mode = inject_mode
        self.latency_ms = latency_ms
        self.custom_intent = custom_intent
        self.predicted_primitive = predicted_primitive
        self.predicted_goal = predicted_goal
        self.predicted_clarity = predicted_clarity
        self.release_version = release_version
    }
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
    let isEditable: Bool
}
