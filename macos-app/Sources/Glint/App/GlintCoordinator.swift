import AppKit
import ApplicationServices
import Combine
import Foundation
import ServiceManagement
import SwiftUI

/// Orchestrates the end-to-end Phase 1 Glint loop:
/// Option+Space -> safe context capture -> brain-server /route -> 3-card FloatingPanel
/// -> keyboard/mouse pick -> safe TextInjector -> async /feedback.
@MainActor
final class GlintCoordinator: ObservableObject {
    @Published private(set) var isAccessibilityGranted: Bool = AXIsProcessTrusted()
    @Published private(set) var isHotkeyRunning: Bool = false
    @Published private(set) var isRouting: Bool = false
    @Published private(set) var isLaunchAtLoginEnabled: Bool = (SMAppService.mainApp.status == .enabled)
    @Published private(set) var backendSummary: String = "检查中…"
    @Published private(set) var lastLatencyMs: Double?
    @Published private(set) var lastStatusMessage: String = "按 Fn+Space 在任意输入框呼出认知卡片"
    @Published var hotkeyPreset: GlintHotkeyPreset {
        didSet {
            UserDefaults.standard.set(hotkeyPreset.rawValue, forKey: Self.hotkeyDefaultsKey)
            inputMonitor.preset = hotkeyPreset
            lastStatusMessage = "快捷键已切换为 \(hotkeyPreset.shortLabel)"
        }
    }

    private static let hotkeyDefaultsKey = "glint.hotkeyPreset"

    private let brainClient = BrainClient()
    private let contextReader = ContextReader()
    private let textInjector = TextInjector()
    private let inputMonitor = HotkeyAndInputMonitor()
    private let panel = FloatingPanel()

    private var activeContext: CapturedContext?
    private var activeResponse: RouteResponse?
    private var readingCard: CardItem?
    private var selectedIndex: Int = 0
    private var isPopupVisible: Bool = false
    private var isEditingCustomIntent: Bool = false
    private var inFlightTask: Task<Void, Never>?
    private var permissionTimer: Timer?
    private var outsideClickMonitor: Any?
    private var appSwitchObserver: NSObjectProtocol?
    private var wakeObserver: NSObjectProtocol?

    init() {
        let savedRaw = UserDefaults.standard.string(forKey: Self.hotkeyDefaultsKey)
        let resolvedPreset = savedRaw.flatMap(GlintHotkeyPreset.init(rawValue:)) ?? .doubleRightOption
        self.hotkeyPreset = resolvedPreset
        self.lastStatusMessage = "按 \(resolvedPreset.shortLabel) 在任意输入框呼出认知卡片"
        inputMonitor.preset = resolvedPreset

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

        if wakeObserver == nil {
            wakeObserver = NSWorkspace.shared.notificationCenter.addObserver(
                forName: NSWorkspace.didWakeNotification,
                object: nil,
                queue: .main
            ) { [weak self] _ in
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    // Quietly re-warm the local 2B model after macOS sleep so the first trigger stays snappy.
                    await self.brainClient.warmup()
                    await self.refreshBackendHealth()
                }
            }
        }
    }

    func stop() {
        permissionTimer?.invalidate()
        permissionTimer = nil
        if let wakeObserver {
            NSWorkspace.shared.notificationCenter.removeObserver(wakeObserver)
            self.wakeObserver = nil
        }
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
        // Avoid triggering redundant macOS prompt dialogs when already trusted.
        if !AXIsProcessTrusted() {
            let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
            AXIsProcessTrustedWithOptions(options)
        }
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility") {
            NSWorkspace.shared.open(url)
        }
    }

    func toggleLaunchAtLogin() {
        do {
            if SMAppService.mainApp.status == .enabled {
                try SMAppService.mainApp.unregister()
            } else {
                try SMAppService.mainApp.register()
            }
            isLaunchAtLoginEnabled = (SMAppService.mainApp.status == .enabled)
            lastStatusMessage = isLaunchAtLoginEnabled ? "已开启登录时自动启动" : "已关闭登录时自动启动"
        } catch {
            isLaunchAtLoginEnabled = (SMAppService.mainApp.status == .enabled)
            lastStatusMessage = "开机自启设置失败：请确保 Glint 位于 /Applications"
            NSLog("[Glint] SMAppService toggle failed: \(error.localizedDescription)")
        }
    }

    func openAdminConsole() {
        if let url = URL(string: "http://127.0.0.1:18765/admin/") {
            NSWorkspace.shared.open(url)
        } else {
            panel.presentToast(message: "无法打开 Glint 管理台", symbol: "exclamationmark.triangle")
        }
    }

    func refreshBackendHealth() async {
        do {
            let health = try await brainClient.health()
            let ollamaLabel = health.ollama_reachable ? "Ollama 在线" : "Ollama 未连"
            backendSummary = "\(health.model) · \(health.cards_loaded) 张卡 · \(ollamaLabel)"
        } catch {
            backendSummary = "未连接 (localhost:18765)"
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
            let outcome = await self.contextReader.captureContext()
            guard !Task.isCancelled else { return }

            let context: CapturedContext
            switch outcome {
            case .captured(let captured):
                context = captured
            case .silentIgnore:
                // Password managers / sensitive apps: stay completely invisible.
                return
            case .blockedSecureField:
                self.lastStatusMessage = "已在密码/敏感输入框自动忽略"
                return
            case .emptyText:
                self.lastStatusMessage = "未检测到可路由的文本"
                self.panel.presentToast(message: "未检测到文本", symbol: "text.cursor")
                return
            }

            self.isRouting = true
            defer { self.isRouting = false }

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
                self.backendSummary = "\(response.model) · 在线 (\(Int(response.latency_ms))ms)"
                self.lastStatusMessage = "已在 \(context.appName) 路由 \(response.cards.count) 张卡片 (\(Int(response.latency_ms))ms)"
                self.showPopup()
            } catch {
                self.backendSummary = "未连接 (localhost:18765)"
                self.lastStatusMessage = "路由失败：请确认 brain-server 已启动 (\(error.localizedDescription))"
                self.panel.presentToast(message: "brain-server 未启动 (18765)", symbol: "bolt.slash")
                NSLog("[Glint] Route failed: \(error.localizedDescription)")
            }
        }
    }

    private func showPopup() {
        guard let response = activeResponse, let context = activeContext else { return }
        isPopupVisible = true
        isEditingCustomIntent = false
        panel.allowsKeyFocus = false
        renderPopup(response: response, context: context)
        inputMonitor.setPopupInterceptionActive(true)
        installDismissMonitors()
    }

    private func installDismissMonitors() {
        removeDismissMonitors()

        outsideClickMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self, self.isPopupVisible else { return }
                let clickPoint = NSEvent.mouseLocation
                if !self.panel.frame.contains(clickPoint) {
                    self.dismissPopup(recordNegativeFeedback: true)
                }
            }
        }

        appSwitchObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification,
            object: nil,
            queue: .main
        ) { [weak self] notification in
            let activatedPID = (notification.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication)?.processIdentifier
            Task { @MainActor [weak self] in
                guard let self, self.isPopupVisible else { return }
                // Ignore activating Glint itself when entering the 4th option's inline text field.
                if activatedPID == ProcessInfo.processInfo.processIdentifier {
                    return
                }
                self.dismissPopup(recordNegativeFeedback: true)
            }
        }
    }

    private func removeDismissMonitors() {
        if let monitor = outsideClickMonitor {
            NSEvent.removeMonitor(monitor)
            outsideClickMonitor = nil
        }
        if let observer = appSwitchObserver {
            NSWorkspace.shared.notificationCenter.removeObserver(observer)
            appSwitchObserver = nil
        }
    }

    private func renderPopup(response: RouteResponse, context: CapturedContext) {
        let view = CardPopupView(
            cards: response.cards,
            selectedIndex: selectedIndex,
            latencyMs: response.latency_ms,
            appName: context.appName,
            isEditingCustomIntent: isEditingCustomIntent,
            onSelect: { [weak self] idx, forceWrap in
                self?.commitSelection(index: idx, forceWrap: forceWrap)
            },
            onOpenCustomInput: { [weak self] in
                self?.enterCustomIntentMode()
            },
            onSubmitCustomIntent: { [weak self] customText, shouldInject in
                self?.commitCustomIntent(customText, shouldInject: shouldInject)
            },
            onDismiss: { [weak self] in
                self?.dismissPopup(recordNegativeFeedback: true)
            }
        )
        panel.present(rootView: view, anchorAXRect: context.anchorRect)
    }

    private func handlePopupKeyCommand(_ command: PopupKeyCommand) {
        guard isPopupVisible, !isEditingCustomIntent,
              let response = activeResponse, let context = activeContext else { return }

        // Reader overlay mode for show_checklist / open_resource (never overwrites user input)
        if let card = readingCard {
            switch command {
            case .confirm(let forceWrap):
                if forceWrap {
                    injectReadingCard(card, context: context)
                } else {
                    copyReadingCard(card)
                }
            case .cancel, .dismissAndPassThrough:
                dismissPopup(recordNegativeFeedback: false)
            default:
                break
            }
            return
        }

        let cardCount = response.cards.count
        let totalOptions = cardCount + 1 // 3 cards + 4th custom intent option
        guard cardCount > 0 else { return }

        switch command {
        case .selectIndex(let idx):
            guard idx < cardCount else { return }
            commitSelection(index: idx, forceWrap: false)
        case .openCustomInput:
            enterCustomIntentMode()
        case .moveUp:
            selectedIndex = (selectedIndex - 1 + totalOptions) % totalOptions
            renderPopup(response: response, context: context)
        case .moveDown:
            selectedIndex = (selectedIndex + 1) % totalOptions
            renderPopup(response: response, context: context)
        case .confirm(let forceWrap):
            if selectedIndex == cardCount {
                enterCustomIntentMode()
            } else {
                commitSelection(index: selectedIndex, forceWrap: forceWrap)
            }
        case .cancel:
            dismissPopup(recordNegativeFeedback: true)
        case .dismissAndPassThrough:
            dismissPopup(recordNegativeFeedback: true)
        }
    }

    private func enterCustomIntentMode() {
        guard isPopupVisible, let response = activeResponse, let context = activeContext else { return }
        isEditingCustomIntent = true
        selectedIndex = response.cards.count
        // Release CGEventTap interception so the inline NSTextField & Chinese IME receive all keys normally.
        inputMonitor.setPopupInterceptionActive(false)
        panel.allowsKeyFocus = true
        renderPopup(response: response, context: context)
    }

    private func commitCustomIntent(_ customText: String, shouldInject: Bool) {
        guard let response = activeResponse, let context = activeContext else {
            dismissPopup(recordNegativeFeedback: false)
            return
        }

        let trimmed = customText.trimmingCharacters(in: .whitespacesAndNewlines)
        dismissPopup(recordNegativeFeedback: false)

        // Return focus back to the original host app cleanly.
        if let hostApp = NSRunningApplication(processIdentifier: context.appPID) {
            hostApp.activate(options: [])
        }

        if shouldInject && !trimmed.isEmpty {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.08) { [weak self] in
                guard let self else { return }
                let outcome = self.textInjector.injectRawAppend(trimmed, context: context)
                if outcome == .copiedToClipboard {
                    self.panel.presentToast(message: "已复制，可 ⌘V 粘贴", symbol: "doc.on.clipboard")
                }
            }
            lastStatusMessage = "已记录并追加自定义指令"
        } else {
            lastStatusMessage = "已记录自定义需求到 usage.db"
            panel.presentToast(message: "已记录你的需求，用于后续迭代", symbol: "checkmark.circle")
        }

        let feedback = FeedbackRequest(
            app_name: context.appName,
            skill_type: response.skill_type,
            input_text: context.inputText,
            recommended_ids: response.cards.map(\.id),
            selected_id: "__custom__",
            inject_mode: shouldInject ? InjectMode.append.rawValue : nil,
            latency_ms: response.latency_ms,
            custom_intent: trimmed,
            predicted_primitive: response.intent?.primitive,
            predicted_goal: response.intent?.goal,
            predicted_clarity: response.intent?.clarity,
            release_version: response.release_version
        )
        Task { await brainClient.sendFeedback(feedback) }
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

        let feedback = FeedbackRequest(
            app_name: context.appName,
            skill_type: response.skill_type,
            input_text: context.inputText,
            recommended_ids: response.cards.map(\.id),
            selected_id: card.id,
            inject_mode: card.isReaderAction && !forceWrap ? nil : effectiveMode.rawValue,
            latency_ms: response.latency_ms,
            predicted_primitive: response.intent?.primitive,
            predicted_goal: response.intent?.goal,
            predicted_clarity: response.intent?.clarity,
            release_version: response.release_version
        )
        Task { await brainClient.sendFeedback(feedback) }

        // Multi-action protocol: show_checklist & open_resource open light reader without overwriting input
        if card.isReaderAction && !forceWrap {
            readingCard = card
            lastStatusMessage = "正在阅读「\(card.title)」(不覆盖输入框)"
            let readerView = LensReaderView(
                card: card,
                onCopy: { [weak self] in
                    self?.copyReadingCard(card)
                },
                onInject: { [weak self] in
                    self?.injectReadingCard(card, context: context)
                },
                onClose: { [weak self] in
                    self?.dismissPopup(recordNegativeFeedback: false)
                }
            )
            panel.present(rootView: readerView, anchorAXRect: context.anchorRect)
            return
        }

        // Close popup and release key interception before injecting text.
        dismissPopup(recordNegativeFeedback: false)
        let injectOutcome = textInjector.inject(card: card, mode: effectiveMode, context: context)
        if injectOutcome == .copiedToClipboard {
            lastStatusMessage = "只读区域：已复制「\(card.title)」完整指令"
            panel.presentToast(message: "已复制，可 ⌘V 粘贴", symbol: "doc.on.clipboard")
        } else {
            lastStatusMessage = "已注入「\(card.title)」(\(effectiveMode.rawValue))"
        }
    }

    private func copyReadingCard(_ card: CardItem) {
        let text = card.prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        dismissPopup(recordNegativeFeedback: false)
        lastStatusMessage = "已复制「\(card.title)」到剪贴板"
        panel.presentToast(message: "已复制「\(card.title)」", symbol: "doc.on.clipboard")
    }

    private func injectReadingCard(_ card: CardItem, context: CapturedContext) {
        dismissPopup(recordNegativeFeedback: false)
        let outcome = textInjector.inject(card: card, mode: .append, context: context)
        if outcome == .copiedToClipboard {
            panel.presentToast(message: "已复制，可 ⌘V 粘贴", symbol: "doc.on.clipboard")
        }
        lastStatusMessage = "已追加注入「\(card.title)」"
    }

    private func dismissPopup(recordNegativeFeedback: Bool) {
        let prevContext = activeContext
        let prevResponse = activeResponse
        let wasEditingCustom = isEditingCustomIntent

        isPopupVisible = false
        isEditingCustomIntent = false
        readingCard = nil
        removeDismissMonitors()
        inputMonitor.setPopupInterceptionActive(false)
        panel.dismiss()
        activeContext = nil
        activeResponse = nil

        if wasEditingCustom, let context = prevContext,
           let hostApp = NSRunningApplication(processIdentifier: context.appPID) {
            hostApp.activate(options: [])
        }

        if recordNegativeFeedback, let context = prevContext, let response = prevResponse {
            lastStatusMessage = "已取消（已记录负反馈）"
            let feedback = FeedbackRequest(
                app_name: context.appName,
                skill_type: response.skill_type,
                input_text: context.inputText,
                recommended_ids: response.cards.map(\.id),
                selected_id: nil,
                inject_mode: nil,
                latency_ms: response.latency_ms,
                predicted_primitive: response.intent?.primitive,
                predicted_goal: response.intent?.goal,
                predicted_clarity: response.intent?.clarity,
                release_version: response.release_version
            )
            Task { await brainClient.sendFeedback(feedback) }
        }
    }
}
