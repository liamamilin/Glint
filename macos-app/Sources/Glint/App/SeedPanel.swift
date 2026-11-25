import AppKit
import SwiftUI

/// Always-on, non-activating, draggable frosted-glass Ambient HUD panel for Glint Seed.
/// Never steals keyboard focus or active app status; remembers its desktop position.
@MainActor
final class SeedPanel: NSPanel {
    private static let originXKey = "glint.seed.originX"
    private static let originYKey = "glint.seed.originY"

    private var moveObserver: NSObjectProtocol?
    private var fadeGeneration: Int = 0

    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }

    init() {
        super.init(
            contentRect: NSRect(x: 0, y: 0, width: 320, height: 112),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        level = .floating
        isOpaque = false
        backgroundColor = .clear
        hasShadow = true
        hidesOnDeactivate = false
        isMovableByWindowBackground = true
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        animationBehavior = .utilityWindow

        moveObserver = NotificationCenter.default.addObserver(
            forName: NSWindow.didMoveNotification,
            object: self,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.persistOrigin()
            }
        }
    }

    deinit {
        if let moveObserver {
            NotificationCenter.default.removeObserver(moveObserver)
        }
    }

    func mount<Content: View>(rootView: Content) {
        let hosting = NSHostingView(rootView: rootView)
        hosting.translatesAutoresizingMaskIntoConstraints = false
        contentView = hosting
        resizeToFit(preserveOrigin: true)
    }

    func resizeToFit(preserveOrigin: Bool) {
        guard let hosting = contentView else { return }
        hosting.layoutSubtreeIfNeeded()
        let fitting = hosting.fittingSize
        let size = CGSize(
            width: max(320, ceil(fitting.width)),
            height: max(88, ceil(fitting.height))
        )
        let currentOrigin = frame.origin
        setContentSize(size)
        if preserveOrigin && hasPersistedOrigin {
            setFrameOrigin(clampedOrigin(currentOrigin, size: size))
        } else {
            setFrameOrigin(restoredOrDefaultOrigin(size: size))
        }
    }

    func showAmbient() {
        fadeGeneration += 1
        resizeToFit(preserveOrigin: isVisible)
        if !isVisible {
            alphaValue = 0.0
            orderFrontRegardless()
            NSAnimationContext.runAnimationGroup { ctx in
                ctx.duration = 0.22
                ctx.timingFunction = CAMediaTimingFunction(name: .easeOut)
                self.animator().alphaValue = 1.0
            }
        } else {
            alphaValue = 1.0
            orderFrontRegardless()
        }
    }

    func hideAmbient() {
        fadeGeneration += 1
        let gen = fadeGeneration
        guard isVisible else {
            orderOut(nil)
            return
        }
        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = 0.14
            ctx.timingFunction = CAMediaTimingFunction(name: .easeIn)
            self.animator().alphaValue = 0.0
        }, completionHandler: { [weak self] in
            Task { @MainActor [weak self] in
                guard let self, self.fadeGeneration == gen else { return }
                self.orderOut(nil)
            }
        })
    }

    func resetToDefaultCorner() {
        UserDefaults.standard.removeObject(forKey: Self.originXKey)
        UserDefaults.standard.removeObject(forKey: Self.originYKey)
        let origin = defaultBottomRightOrigin(size: frame.size)
        setFrameOrigin(origin)
        persistOrigin()
    }

    private var hasPersistedOrigin: Bool {
        UserDefaults.standard.object(forKey: Self.originXKey) != nil &&
        UserDefaults.standard.object(forKey: Self.originYKey) != nil
    }

    private func persistOrigin() {
        guard isVisible else { return }
        UserDefaults.standard.set(Double(frame.origin.x), forKey: Self.originXKey)
        UserDefaults.standard.set(Double(frame.origin.y), forKey: Self.originYKey)
    }

    private func restoredOrDefaultOrigin(size: CGSize) -> NSPoint {
        if hasPersistedOrigin {
            let x = UserDefaults.standard.double(forKey: Self.originXKey)
            let y = UserDefaults.standard.double(forKey: Self.originYKey)
            return clampedOrigin(NSPoint(x: x, y: y), size: size)
        }
        return defaultBottomRightOrigin(size: size)
    }

    private func defaultBottomRightOrigin(size: CGSize) -> NSPoint {
        let screen = NSScreen.main ?? NSScreen.screens.first
        let visible = screen?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let x = visible.maxX - size.width - 20
        let y = visible.minY + 20
        return NSPoint(x: round(x), y: round(y))
    }

    private func clampedOrigin(_ origin: NSPoint, size: CGSize) -> NSPoint {
        let center = NSPoint(x: origin.x + size.width / 2, y: origin.y + size.height / 2)
        let screen = NSScreen.screens.first { $0.frame.contains(center) }
            ?? NSScreen.main
            ?? NSScreen.screens.first
        let visible = screen?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let x = min(max(origin.x, visible.minX + 10), visible.maxX - size.width - 10)
        let y = min(max(origin.y, visible.minY + 10), visible.maxY - size.height - 10)
        return NSPoint(x: round(x), y: round(y))
    }
}

/// Reader panel anchored cleanly near the Seed HUD when the user clicks "展开本体".
/// Supports inline variable fill-in when explicitly opened, while SeedPanel itself never steals focus.
@MainActor
final class SeedReaderPanel: NSPanel {
    private var fadeGeneration: Int = 0

    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }

    init() {
        super.init(
            contentRect: NSRect(x: 0, y: 0, width: 400, height: 320),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        level = .floating
        isOpaque = false
        backgroundColor = .clear
        hasShadow = true
        hidesOnDeactivate = false
        isMovableByWindowBackground = true
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        animationBehavior = .utilityWindow
    }

    func present<Content: View>(rootView: Content, anchoredTo anchorFrame: NSRect) {
        fadeGeneration += 1
        let hosting = NSHostingView(rootView: rootView)
        hosting.frame = NSRect(x: 0, y: 0, width: 400, height: 460)
        hosting.layoutSubtreeIfNeeded()
        let fitting = hosting.fittingSize
        let size = CGSize(width: 400, height: max(180, min(520, ceil(fitting.height))))
        hosting.frame = NSRect(origin: .zero, size: size)
        contentView = hosting
        setContentSize(size)

        let center = NSPoint(x: anchorFrame.midX, y: anchorFrame.midY)
        let screen = NSScreen.screens.first { $0.frame.contains(center) }
            ?? NSScreen.main
            ?? NSScreen.screens.first
        let visible = screen?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)

        var x = anchorFrame.maxX - size.width
        var y = anchorFrame.maxY + 10
        if y + size.height > visible.maxY - 12 {
            y = anchorFrame.minY - size.height - 10
        }
        x = min(max(x, visible.minX + 12), visible.maxX - size.width - 12)
        y = min(max(y, visible.minY + 12), visible.maxY - size.height - 12)
        setFrameOrigin(NSPoint(x: round(x), y: round(y)))

        if !isVisible {
            alphaValue = 0.0
            orderFrontRegardless()
            NSAnimationContext.runAnimationGroup { ctx in
                ctx.duration = 0.14
                ctx.timingFunction = CAMediaTimingFunction(name: .easeOut)
                self.animator().alphaValue = 1.0
            }
        } else {
            alphaValue = 1.0
            orderFrontRegardless()
        }
    }

    func dismiss() {
        fadeGeneration += 1
        let gen = fadeGeneration
        guard isVisible else {
            orderOut(nil)
            return
        }
        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = 0.10
            ctx.timingFunction = CAMediaTimingFunction(name: .easeIn)
            self.animator().alphaValue = 0.0
        }, completionHandler: { [weak self] in
            Task { @MainActor [weak self] in
                guard let self, self.fadeGeneration == gen else { return }
                self.orderOut(nil)
            }
        })
    }
}
