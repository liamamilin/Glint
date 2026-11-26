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

// MARK: - Glint Seed (Ambient HUD Carousel)

enum SeedPoolMode: String, CaseIterable, Identifiable, Codable, Sendable {
    case smartMix = "smart_mix"
    case l4All = "l4_all"
    case l3Discovery = "l3_discovery"
    case tierS = "tier_s"

    var id: String { rawValue }

    var label: String {
        switch self {
        case .smartMix: return "全域智能混播 (L4 + L3探索)"
        case .l4All: return "仅 L4 在架 (S/A/B加权)"
        case .l3Discovery: return "仅 L3 休眠探索"
        case .tierS: return "仅 S 级核心心法"
        }
    }

    var shortLabel: String {
        switch self {
        case .smartMix: return "智能混播"
        case .l4All: return "L4 在架"
        case .l3Discovery: return "L3 探索"
        case .tierS: return "S 级核心"
        }
    }
}

struct SeedItem: Codable, Identifiable, Equatable, Sendable {
    let id: String
    let title: String
    let tagline: String
    let pain_point: String
    let prompt: String
    let primitive: String
    let category: String
    let tier: String
    let layer: String?
    let source: String
    let trigger_utterances: [String]
    let weight: Double?
    let probability_pct: Double?
    let recall_count_30d: Int?
    let reason_tag: String?

    var isL3Discovery: Bool {
        (layer ?? "L4").uppercased() == "L3"
    }
}

struct SeedPoolResponse: Codable, Sendable {
    let items: [SeedItem]
    let mode: String?
    let total_l4: Int
    let total_l3: Int?
    let muted_count: Int
    let snoozed_count: Int
    let release_version: String?
}

struct SeedDrawRequest: Codable, Sendable {
    let mode: String
    let exclude_ids: [String]
}

struct SeedDrawResponse: Codable, Sendable {
    let item: SeedItem?
    let mode: String
    let eligible_count: Int
    let total_l4: Int
    let total_l3: Int
    let muted_count: Int
    let snoozed_count: Int
    let release_version: String?
}

struct SeedFeedbackRequest: Codable, Sendable {
    let id: String
    let action: String
    let snooze_days: Int
    let category: String?

    init(id: String, action: String, snoozeDays: Int = 7, category: String? = nil) {
        self.id = id
        self.action = action
        self.snooze_days = snoozeDays
        self.category = category
    }
}

struct SeedFeedbackResponse: Codable, Sendable {
    let ok: Bool
    let muted_count: Int
    let snoozed_count: Int
}

enum SeedOpacityPreset: String, CaseIterable, Identifiable {
    case whisper
    case standard
    case clear

    var id: String { rawValue }

    var label: String {
        switch self {
        case .whisper: return "轻透玻璃"
        case .standard: return "标准磨砂"
        case .clear: return "清晰高对比"
        }
    }

    /// Controls dark/light tint density over the frosted material so text stays crisp even on pure white pages.
    var backdropAlpha: Double {
        switch self {
        case .whisper: return 0.62
        case .standard: return 0.74
        case .clear: return 0.86
        }
    }

    var textAlpha: Double {
        switch self {
        case .whisper: return 0.90
        case .standard: return 0.96
        case .clear: return 1.0
        }
    }
}
