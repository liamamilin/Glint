import AppKit
import Combine
import Foundation
import SwiftUI

/// Drives the Glint Seed Ambient HUD carousel with server-side probabilistic sampling:
/// - Supports pool modes (smart_mix L4+L3 / l4_all / l3_discovery / tier_s)
/// - Auto-draws every 5 minutes (300s) using live tier weights + active recall decay + user behavior
/// - Keeps a local history stack for instant ← / → navigation
@MainActor
final class SeedController: ObservableObject {
    static let defaultIntervalSeconds: Double = 300.0 // 5 minutes
    private static let maxHistoryCount: Int = 30

    private static let enabledKey = "glint.seed.enabled"
    private static let opacityKey = "glint.seed.opacityPreset"
    private static let poolModeKey = "glint.seed.poolMode"

    @Published var isEnabled: Bool {
        didSet {
            UserDefaults.standard.set(isEnabled, forKey: Self.enabledKey)
            if isEnabled {
                isHovering = false
                showIfReady()
                if currentSeed == nil {
                    Task { await drawNextFromServer(excludeCurrent: false) }
                }
            } else {
                isHovering = false
                closeReader()
                seedPanel.hideAmbient()
            }
        }
    }

    @Published var opacityPreset: SeedOpacityPreset {
        didSet {
            UserDefaults.standard.set(opacityPreset.rawValue, forKey: Self.opacityKey)
        }
    }

    @Published var poolMode: SeedPoolMode {
        didSet {
            guard oldValue != poolMode else { return }
            UserDefaults.standard.set(poolMode.rawValue, forKey: Self.poolModeKey)
            history.removeAll()
            historyIndex = -1
            resetTimerProgress()
            Task { await drawNextFromServer(excludeCurrent: false) }
        }
    }

    @Published private(set) var history: [SeedItem] = []
    @Published private(set) var historyIndex: Int = -1
    @Published private(set) var eligibleCount: Int = 0
    @Published private(set) var totalL4Count: Int = 0
    @Published private(set) var totalL3Count: Int = 0
    @Published private(set) var mutedCount: Int = 0
    @Published private(set) var snoozedCount: Int = 0
    @Published private(set) var pinnedCount: Int = 0
    @Published private(set) var isHovering: Bool = false
    @Published private(set) var isReaderOpen: Bool = false
    @Published private(set) var progress: Double = 0.0
    @Published private(set) var copiedFeedback: Bool = false
    @Published private(set) var copyFeedbackText: String = "已复制"
    @Published private(set) var isLoading: Bool = false
    @Published private(set) var undoBanner: (item: SeedItem, label: String)?

    private let brainClient: BrainClient
    private let seedPanel = SeedPanel()
    private let readerPanel = SeedReaderPanel()

    private var elapsedSeconds: Double = 0.0
    private var drawGeneration: Int = 0
    private var undoWorkItem: DispatchWorkItem?
    private var tickTimer: Timer?
    private var retryTimer: Timer?
    private var readerOutsideClickMonitor: Any?
    private var isMounted: Bool = false

    var currentSeed: SeedItem? {
        guard historyIndex >= 0, historyIndex < history.count else { return nil }
        return history[historyIndex]
    }

    var canGoPrev: Bool {
        historyIndex > 0
    }

    var hiddenCount: Int {
        mutedCount + snoozedCount
    }

    var remainingFormatted: String {
        let rem = max(0, Int(ceil(Self.defaultIntervalSeconds - elapsedSeconds)))
        let mins = rem / 60
        let secs = rem % 60
        return String(format: "%d:%02d", mins, secs)
    }

    init(brainClient: BrainClient) {
        self.brainClient = brainClient

        if UserDefaults.standard.object(forKey: Self.enabledKey) == nil {
            self.isEnabled = true
        } else {
            self.isEnabled = UserDefaults.standard.bool(forKey: Self.enabledKey)
        }

        let rawPreset = UserDefaults.standard.string(forKey: Self.opacityKey)
        self.opacityPreset = rawPreset.flatMap(SeedOpacityPreset.init(rawValue:)) ?? .standard

        let rawMode = UserDefaults.standard.string(forKey: Self.poolModeKey)
        self.poolMode = rawMode.flatMap(SeedPoolMode.init(rawValue:)) ?? .smartMix

        self.readerPanel.onCancelEscape = { [weak self] in
            self?.closeReader()
        }
    }

    func start() {
        mountHUDIfNeeded()
        startTicker()
        Task { await refreshPool(preserveCurrentId: true) }
    }

    func stop() {
        isHovering = false
        tickTimer?.invalidate()
        tickTimer = nil
        retryTimer?.invalidate()
        retryTimer = nil
        closeReader()
        seedPanel.hideAmbient()
    }

    func refreshPool(preserveCurrentId: Bool = true) async {
        if preserveCurrentId && currentSeed != nil {
            if isEnabled {
                showIfReady()
            }
            return
        }
        await drawNextFromServer(excludeCurrent: false)
    }

    func setHovering(_ hovering: Bool) {
        isHovering = hovering
    }

    /// Manual next (→) or automatic 5-min advance.
    func next(manual: Bool = true) {
        // If browsing history, step forward cleanly without drawing a new card
        if historyIndex >= 0 && historyIndex + 1 < history.count {
            historyIndex += 1
            resetTimerProgress()
            copiedFeedback = false
            syncHUDFrame()
            if isReaderOpen { renderReader() }
            return
        }

        // If user manually skips a freshly shown card within 45s, record gentle skip decay
        if manual, let skipped = currentSeed, elapsedSeconds < 45.0 {
            Task {
                _ = await brainClient.sendSeedFeedback(id: skipped.id, action: "skip")
            }
        }

        Task { await drawNextFromServer(excludeCurrent: true) }
    }

    func prev() {
        guard historyIndex > 0 else { return }
        historyIndex -= 1
        resetTimerProgress()
        copiedFeedback = false
        syncHUDFrame()
        if isReaderOpen { renderReader() }
    }

    func snoozeCurrent(days: Int = 7) {
        guard let item = currentSeed else { return }
        snoozedCount += 1
        showUndoBanner(item: item, label: "已 7 天不看「\(item.title)」")
        Task {
            if let res = await brainClient.sendSeedFeedback(id: item.id, action: "snooze", snoozeDays: days) {
                self.mutedCount = res.muted_count
                self.snoozedCount = res.snoozed_count
            }
            removeFromHistory(id: item.id)
            await drawNextFromServer(excludeCurrent: false, extraExcludeIds: [item.id])
        }
    }

    func togglePinCurrent() {
        guard historyIndex >= 0, historyIndex < history.count else { return }
        let item = history[historyIndex]
        let nextPinned = !item.isPinned
        history[historyIndex] = item.withPinned(nextPinned)
        if isReaderOpen { renderReader() }
        Task {
            if let res = await brainClient.sendSeedFeedback(id: item.id, action: nextPinned ? "pin" : "unpin") {
                self.mutedCount = res.muted_count
                self.snoozedCount = res.snoozed_count
                if let pc = res.pinned_count {
                    self.pinnedCount = pc
                }
            }
            if !nextPinned && self.poolMode == .pinnedOnly {
                removeFromHistory(id: item.id)
                await drawNextFromServer(excludeCurrent: false, extraExcludeIds: [item.id])
            }
        }
    }

    func muteCurrent() {
        guard let item = currentSeed else { return }
        mutedCount += 1
        showUndoBanner(item: item, label: "已不再轮播「\(item.title)」")
        Task {
            if let res = await brainClient.sendSeedFeedback(id: item.id, action: "mute") {
                self.mutedCount = res.muted_count
                self.snoozedCount = res.snoozed_count
                if let pc = res.pinned_count { self.pinnedCount = pc }
            }
            removeFromHistory(id: item.id)
            await drawNextFromServer(excludeCurrent: false, extraExcludeIds: [item.id])
        }
    }

    func undoLastHide() {
        guard let pending = undoBanner else { return }
        undoWorkItem?.cancel()
        undoWorkItem = nil
        undoBanner = nil
        let restored = pending.item
        Task {
            if let res = await brainClient.sendSeedFeedback(id: restored.id, action: "unmute") {
                self.mutedCount = res.muted_count
                self.snoozedCount = res.snoozed_count
            }
            history.append(restored)
            if history.count > Self.maxHistoryCount {
                history.removeFirst(history.count - Self.maxHistoryCount)
            }
            historyIndex = history.count - 1
            resetTimerProgress()
            syncHUDFrame()
            if isReaderOpen { renderReader() }
        }
    }

    private func showUndoBanner(item: SeedItem, label: String) {
        undoWorkItem?.cancel()
        undoBanner = (item: item, label: label)
        syncHUDFrame()
        let work = DispatchWorkItem { [weak self] in
            Task { @MainActor [weak self] in
                self?.undoBanner = nil
                self?.syncHUDFrame()
            }
        }
        undoWorkItem = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 4.5, execute: work)
    }

    func resetAllHidden() {
        Task {
            if let res = await brainClient.resetSeedState() {
                self.mutedCount = res.muted_count
                self.snoozedCount = res.snoozed_count
            }
            history.removeAll()
            historyIndex = -1
            await drawNextFromServer(excludeCurrent: false)
        }
    }

    func openReader() {
        guard let item = currentSeed else { return }
        isReaderOpen = true
        copiedFeedback = false
        renderReader()
        installReaderOutsideClickMonitor()
        // Record positive expand signal (boosts category affinity gently, clears skip penalty)
        Task {
            _ = await brainClient.sendSeedFeedback(
                id: item.id,
                action: "expand",
                category: item.category
            )
        }
    }

    func closeReader() {
        isReaderOpen = false
        copiedFeedback = false
        removeReaderOutsideClickMonitor()
        readerPanel.dismiss()
    }

    func toggleReader() {
        if isReaderOpen {
            closeReader()
        } else {
            openReader()
        }
    }

    func copyCurrentPrompt() {
        guard let item = currentSeed else { return }
        let cleaned = item.prompt
            .replacingOccurrences(of: "{{input}}", with: "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        copyPromptText(cleaned.isEmpty ? item.title : cleaned)
    }

    func copyPromptText(_ text: String, feedbackLabel: String = "已复制") {
        guard let item = currentSeed else { return }
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.setString(text.isEmpty ? item.title : text, forType: .string)
        copyFeedbackText = feedbackLabel
        copiedFeedback = true
        Task {
            _ = await brainClient.sendSeedFeedback(
                id: item.id,
                action: "copy",
                category: item.category
            )
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) { [weak self] in
            self?.copiedFeedback = false
        }
    }

    func resetPosition() {
        seedPanel.resetToDefaultCorner()
        if isReaderOpen {
            renderReader()
        }
    }

    // MARK: - Private

    private func drawNextFromServer(excludeCurrent: Bool, extraExcludeIds: [String] = []) async {
        drawGeneration += 1
        let gen = drawGeneration
        isLoading = true
        defer {
            if drawGeneration == gen {
                isLoading = false
            }
        }

        var excludeIds = extraExcludeIds
        if excludeCurrent, let cur = currentSeed?.id {
            excludeIds.append(cur)
        }
        let requestedMode = poolMode
        do {
            let res = try await brainClient.drawNextSeed(mode: requestedMode, excludeIds: excludeIds)
            guard drawGeneration == gen else { return }
            retryTimer?.invalidate()
            retryTimer = nil

            eligibleCount = res.eligible_count
            totalL4Count = res.total_l4
            totalL3Count = res.total_l3
            mutedCount = res.muted_count
            snoozedCount = res.snoozed_count
            if let pc = res.pinned_count { pinnedCount = pc }

            if let item = res.item {
                if historyIndex >= 0 && historyIndex + 1 < history.count {
                    history.removeSubrange((historyIndex + 1)..<history.count)
                }
                history.append(item)
                if history.count > Self.maxHistoryCount {
                    history.removeFirst(history.count - Self.maxHistoryCount)
                }
                historyIndex = history.count - 1
                resetTimerProgress()
                copiedFeedback = false
                if isEnabled {
                    showIfReady()
                    syncHUDFrame()
                }
                if isReaderOpen {
                    renderReader()
                }
            } else {
                history.removeAll()
                historyIndex = -1
                closeReader()
                if isEnabled {
                    showIfReady()
                    syncHUDFrame()
                }
            }
        } catch {
            guard drawGeneration == gen else { return }
            NSLog("[Glint Seed] Failed to draw next seed: \(error.localizedDescription)")
            scheduleRetry()
        }
    }

    private func removeFromHistory(id: String) {
        history.removeAll { $0.id == id }
        if history.isEmpty {
            historyIndex = -1
        } else if historyIndex >= history.count {
            historyIndex = history.count - 1
        }
    }

    private func syncHUDFrame() {
        DispatchQueue.main.async { [weak self] in
            guard let self, self.isEnabled else { return }
            self.seedPanel.resizeToFit(preserveOrigin: true)
            if self.isReaderOpen {
                self.renderReader()
            }
        }
    }

    private func mountHUDIfNeeded() {
        guard !isMounted else { return }
        isMounted = true
        seedPanel.mount(rootView: SeedHUDView(controller: self))
    }

    private func showIfReady() {
        guard isEnabled else { return }
        mountHUDIfNeeded()
        seedPanel.showAmbient()
    }

    private func resetTimerProgress() {
        elapsedSeconds = 0.0
        progress = 0.0
    }

    private func startTicker() {
        tickTimer?.invalidate()
        tickTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.tick()
            }
        }
    }

    private func tick() {
        guard isEnabled, currentSeed != nil, !isHovering, !isReaderOpen else { return }
        elapsedSeconds += 1.0
        if elapsedSeconds >= Self.defaultIntervalSeconds {
            next(manual: false)
        } else {
            progress = min(1.0, elapsedSeconds / Self.defaultIntervalSeconds)
        }
    }

    private func scheduleRetry() {
        guard retryTimer == nil else { return }
        retryTimer = Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                await self?.drawNextFromServer(excludeCurrent: false)
            }
        }
    }

    private func renderReader() {
        guard currentSeed != nil else {
            closeReader()
            return
        }
        readerPanel.present(
            rootView: SeedReaderView(controller: self),
            anchoredTo: seedPanel.frame
        )
    }

    private func installReaderOutsideClickMonitor() {
        removeReaderOutsideClickMonitor()
        readerOutsideClickMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self, self.isReaderOpen else { return }
                let pt = NSEvent.mouseLocation
                if !self.readerPanel.frame.contains(pt) && !self.seedPanel.frame.contains(pt) {
                    self.closeReader()
                }
            }
        }
    }

    private func removeReaderOutsideClickMonitor() {
        if let monitor = readerOutsideClickMonitor {
            NSEvent.removeMonitor(monitor)
            readerOutsideClickMonitor = nil
        }
    }
}
