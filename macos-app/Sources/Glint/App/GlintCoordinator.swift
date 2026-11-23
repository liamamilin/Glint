import AppKit
import ApplicationServices
import Combine
import Foundation
import SwiftUI

/// Orchestrates the end-to-end Phase 1 Glint loop:
/// Option+Space -> safe context capture -> brain-server /route -> 3-card FloatingPanel
/// -> keyboard/mouse pick -> safe TextInjector -> async /feedback.
@MainActor
final class GlintCoordinator: ObservableObject {
    @Published private(set) var isAccessibilityGranted: Bool = AXIsProcessTrusted()
    @Published private(set) var isHotkeyRunning: Bool = false
    @Published private(set) var backendSummary: String = "检查中…"
    @Published private(set) var lastLatencyMs: Double?
    @Published private(set) var lastStatusMessage: String = "按 ⌥Space 在任意输入框呼出认知卡片"

    private let brainClient = BrainClient()
    private let contextReader = ContextReader()
    private let textInjector = TextInjector()
    private let inputMonitor = HotkeyAndInputMonitor()
    private let panel = FloatingPanel()

    private var activeContext: CapturedContext?
    private var activeResponse: RouteResponse?
    private var selectedIndex: Int = 0
    private var isPopupVisible: Bool = false
    private var inFlightTask: Task<Void, Never>?
    private var permissionTimer: Timer?

    init() {
        inputMonitor.onTriggerHotkey = { [weak self] in
            self?.handleTriggerHotkey()
        }
        inputMonitor.onPopupKeyCommand = { [weak self] command in
            self?.handlePopupKeyCommand(command)
        }
        start()
    }

    func start() {
        refreshPermissionsAndTaps()
        Task { await refreshBackendHealth() }

        permissionTimer?.invalidate()
        permissionTimer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.refreshPermissionsAndTaps()
            }
        }
    }

    func stop() {
        permissionTimer?.invalidate()
        permissionTimer = nil
        inFlightTask?.cancel()
        dismissPopup(recordNegativeFeedback: false)
        inputMonitor.stop()
        isHotkeyRunning = false
    }

    func refreshPermissionsAndTaps() {
        let trusted = AXIsProcessTrusted()
        isAccessibilityGranted = trusted
        if trusted {
            if !isHotkeyRunning {
                isHotkeyRunning = inputMonitor.start()
            }
        } else {
            if isHotkeyRunning {
                inputMonitor.stop()
                isHotkeyRunning = false
            }
        }
    }

    func requestAccessibilityPermission() {
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        AXIsProcessTrustedWithOptions(options)
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility") {
            NSWorkspace.shared.open(url)
        }
    }

    func refreshBackendHealth() async {
        do {
            let health = try await brainClient.health()
            let ollamaLabel = health.ollama_reachable ? "Ollama 在线" : "Ollama 未连"
            backendSummary = "\(health.model) · \(health.cards_loaded) 张卡 · \(ollamaLabel)"
        } catch {
            backendSummary = "未连接 (localhost:8765)"
        }
    }

    // MARK: - Trigger & Routing

    private func handleTriggerHotkey() {
        if isPopupVisible {
            dismissPopup(recordNegativeFeedback: true)
            return
        }

        inFlightTask?.cancel()
        inFlightTask = Task { [weak self] in
            guard let self else { return }
            guard let context = await self.contextReader.captureContext() else {
                self.lastStatusMessage = "未读取到可用文本（或当前为敏感密码框）"
                return
            }
            guard !Task.isCancelled else { return }

            do {
                let response = try await self.brainClient.route(
                    inputText: context.inputText,
                    appName: context.appName
                )
                guard !Task.isCancelled, !response.cards.isEmpty else { return }
                self.activeContext = context
                self.activeResponse = response
                self.selectedIndex = 0
                self.lastLatencyMs = response.latency_ms
                self.lastStatusMessage = "已在 \(context.appName) 路由 \(response.cards.count) 张卡片 (\(Int(response.latency_ms))ms)"
                self.showPopup()
            } catch {
                self.lastStatusMessage = "路由失败：请确认 brain-server 已启动 (\(error.localizedDescription))"
                NSLog("[Glint] Route failed: \(error.localizedDescription)")
            }
        }
    }

    private func showPopup() {
        guard let response = activeResponse, let context = activeContext else { return }
        isPopupVisible = true
        renderPopup(response: response, context: context)
        inputMonitor.setPopupInterceptionActive(true)
    }

    private func renderPopup(response: RouteResponse, context: CapturedContext) {
        let view = CardPopupView(
            cards: response.cards,
            selectedIndex: selectedIndex,
            latencyMs: response.latency_ms,
            appName: context.appName,
            onSelect: { [weak self] idx, forceWrap in
                self?.commitSelection(index: idx, forceWrap: forceWrap)
            },
            onDismiss: { [weak self] in
                self?.dismissPopup(recordNegativeFeedback: true)
            }
        )
        panel.present(rootView: view, anchorAXRect: context.anchorRect)
    }

    private func handlePopupKeyCommand(_ command: PopupKeyCommand) {
        guard isPopupVisible, let response = activeResponse, let context = activeContext else { return }
        let count = response.cards.count
        guard count > 0 else { return }

        switch command {
        case .selectIndex(let idx):
            guard idx < count else { return }
            commitSelection(index: idx, forceWrap: false)
        case .moveUp:
            selectedIndex = (selectedIndex - 1 + count) % count
            renderPopup(response: response, context: context)
        case .moveDown:
            selectedIndex = (selectedIndex + 1) % count
            renderPopup(response: response, context: context)
        case .confirm(let forceWrap):
            commitSelection(index: selectedIndex, forceWrap: forceWrap)
        case .cancel:
            dismissPopup(recordNegativeFeedback: true)
        }
    }

    private func commitSelection(index: Int, forceWrap: Bool) {
        guard let response = activeResponse,
              let context = activeContext,
              index >= 0, index < response.cards.count else {
            dismissPopup(recordNegativeFeedback: false)
            return
        }

        let card = response.cards[index]
        let effectiveMode: InjectMode = forceWrap ? .wrap : card.mode

        // Close popup and release key interception before injecting text.
        dismissPopup(recordNegativeFeedback: false)
        textInjector.inject(card: card, mode: effectiveMode, context: context)
        lastStatusMessage = "已注入「\(card.title)」(\(effectiveMode.rawValue))"

        let feedback = FeedbackRequest(
            app_name: context.appName,
            skill_type: response.skill_type,
            input_text: context.inputText,
            recommended_ids: response.cards.map(\.id),
            selected_id: card.id,
            inject_mode: effectiveMode.rawValue,
            latency_ms: response.latency_ms
        )
        Task { await brainClient.sendFeedback(feedback) }
    }

    private func dismissPopup(recordNegativeFeedback: Bool) {
        let prevContext = activeContext
        let prevResponse = activeResponse

        isPopupVisible = false
        inputMonitor.setPopupInterceptionActive(false)
        panel.dismiss()
        activeContext = nil
        activeResponse = nil

        if recordNegativeFeedback, let context = prevContext, let response = prevResponse {
            lastStatusMessage = "已取消（已记录负反馈）"
            let feedback = FeedbackRequest(
                app_name: context.appName,
                skill_type: response.skill_type,
                input_text: context.inputText,
                recommended_ids: response.cards.map(\.id),
                selected_id: nil,
                inject_mode: nil,
                latency_ms: response.latency_ms
            )
            Task { await brainClient.sendFeedback(feedback) }
        }
    }
}
