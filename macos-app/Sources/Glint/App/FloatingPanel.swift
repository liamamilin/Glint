import AppKit
import SwiftUI

/// Borderless, non-activating floating panel that displays Glint's 3 cognitive cards
/// without stealing keyboard focus or caret from the active host application.
@MainActor
final class FloatingPanel: NSPanel {
    /// Stays false during normal 3-card selection so the host app keeps focus;
    /// temporarily flips to true only when the user opens the 4th option's inline text field.
    var allowsKeyFocus: Bool = false

    override var canBecomeKey: Bool { allowsKeyFocus }
    override var canBecomeMain: Bool { false }

    init() {
        super.init(
            contentRect: NSRect(x: 0, y: 0, width: 420, height: 240),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        level = .floating
        isOpaque = false
        backgroundColor = .clear
        hasShadow = true
        hidesOnDeactivate = false
        isMovableByWindowBackground = false
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        animationBehavior = .utilityWindow
    }

    private var toastDismissWorkItem: DispatchWorkItem?
    private var fadeOutGeneration: Int = 0

    func present<Content: View>(
        rootView: Content,
        anchorAXRect: CGRect?,
        fixedWidth: CGFloat? = 380,
        nearPointerFallback: Bool = false
    ) {
        toastDismissWorkItem?.cancel()
        toastDismissWorkItem = nil
        fadeOutGeneration += 1

        let wasVisible = isVisible && alphaValue > 0.5

        let hosting = NSHostingView(rootView: rootView)
        if let fixedWidth {
            hosting.frame = NSRect(x: 0, y: 0, width: fixedWidth, height: 420)
            hosting.layoutSubtreeIfNeeded()
        }
        let fitting = hosting.fittingSize
        let size = CGSize(
            width: fixedWidth ?? max(120, ceil(fitting.width)),
            height: max(30, ceil(fitting.height))
        )
        hosting.frame = NSRect(origin: .zero, size: size)
        contentView = hosting
        setContentSize(size)

        let targetOrigin = resolveOrigin(
            anchorAXRect: anchorAXRect,
            panelSize: size,
            nearPointerFallback: nearPointerFallback
        )
        setFrameOrigin(targetOrigin)

        if !wasVisible {
            alphaValue = 0.0
        }
        if allowsKeyFocus {
            NSApp.activate(ignoringOtherApps: true)
            makeKeyAndOrderFront(nil)
        } else {
            orderFrontRegardless()
        }
        if !wasVisible {
            NSAnimationContext.runAnimationGroup { ctx in
                ctx.duration = 0.11
                ctx.timingFunction = CAMediaTimingFunction(name: .easeOut)
                self.animator().alphaValue = 1.0
            }
        } else {
            alphaValue = 1.0
        }
    }

    func presentToast(message: String, symbol: String = "sparkle", duration: TimeInterval = 0.95) {
        allowsKeyFocus = false
        present(
            rootView: GlintToastView(symbol: symbol, message: message),
            anchorAXRect: nil,
            fixedWidth: nil,
            nearPointerFallback: true
        )
        let work = DispatchWorkItem { [weak self] in
            self?.dismiss(animated: true)
        }
        toastDismissWorkItem = work
        DispatchQueue.main.asyncAfter(deadline: .now() + duration, execute: work)
    }

    func dismiss(animated: Bool = true) {
        toastDismissWorkItem?.cancel()
        toastDismissWorkItem = nil
        allowsKeyFocus = false

        guard isVisible else {
            orderOut(nil)
            return
        }
        if !animated {
            fadeOutGeneration += 1
            orderOut(nil)
            return
        }

        fadeOutGeneration += 1
        let gen = fadeOutGeneration
        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = 0.08
            ctx.timingFunction = CAMediaTimingFunction(name: .easeIn)
            self.animator().alphaValue = 0.0
        }, completionHandler: { [weak self] in
            Task { @MainActor [weak self] in
                guard let self, self.fadeOutGeneration == gen else { return }
                self.orderOut(nil)
            }
        })
    }

    /// Converts an optional top-left AX screen rect into an AppKit bottom-left origin,
    /// clamping inside the visible frame of the active screen.
    private func resolveOrigin(
        anchorAXRect: CGRect?,
        panelSize: CGSize,
        nearPointerFallback: Bool
    ) -> NSPoint {
        let screens = NSScreen.screens
        let primaryMaxY = screens.first?.frame.maxY ?? 0

        if let axRect = anchorAXRect, axRect.width >= 0, axRect.height >= 0 {
            let caretBottomAppKitY = primaryMaxY - axRect.maxY
            let caretTopAppKitY = primaryMaxY - axRect.minY
            let probePoint = NSPoint(x: axRect.minX, y: caretBottomAppKitY)
            let screen = screens.first { $0.frame.contains(probePoint) } ?? NSScreen.main ?? screens.first

            if let visible = screen?.visibleFrame {
                var x = axRect.minX
                var y = caretBottomAppKitY - panelSize.height - 10
                if y < visible.minY + 12 {
                    y = caretTopAppKitY + 10
                }
                x = min(max(x, visible.minX + 12), visible.maxX - panelSize.width - 12)
                y = min(max(y, visible.minY + 12), visible.maxY - panelSize.height - 12)
                return NSPoint(x: round(x), y: round(y))
            }
        }

        let mouseLocation = NSEvent.mouseLocation
        let screen = screens.first { $0.frame.contains(mouseLocation) } ?? NSScreen.main ?? screens.first
        let visible = screen?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)

        if nearPointerFallback {
            var x = mouseLocation.x - panelSize.width / 2
            var y = mouseLocation.y - panelSize.height - 18
            if y < visible.minY + 16 {
                y = mouseLocation.y + 18
            }
            x = min(max(x, visible.minX + 16), visible.maxX - panelSize.width - 16)
            y = min(max(y, visible.minY + 16), visible.maxY - panelSize.height - 16)
            return NSPoint(x: round(x), y: round(y))
        }

        let x = visible.midX - panelSize.width / 2
        let y = visible.midY - panelSize.height / 2 + 48
        return NSPoint(x: round(x), y: round(y))
    }
}
