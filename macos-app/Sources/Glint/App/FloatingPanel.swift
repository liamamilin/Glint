import AppKit
import SwiftUI

/// Borderless, non-activating floating panel that displays Glint's 3 cognitive cards
/// without stealing keyboard focus or caret from the active host application.
@MainActor
final class FloatingPanel: NSPanel {
    override var canBecomeKey: Bool { false }
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

    func present<Content: View>(
        rootView: Content,
        anchorAXRect: CGRect?,
        size: CGSize = CGSize(width: 440, height: 268)
    ) {
        let hosting = NSHostingView(rootView: rootView)
        hosting.frame = NSRect(origin: .zero, size: size)
        contentView = hosting
        setContentSize(size)

        let targetOrigin = resolveOrigin(anchorAXRect: anchorAXRect, panelSize: size)
        setFrameOrigin(targetOrigin)
        orderFrontRegardless()
    }

    func dismiss() {
        orderOut(nil)
    }

    /// Converts an optional top-left AX screen rect into an AppKit bottom-left origin,
    /// clamping inside the visible frame of the active screen.
    private func resolveOrigin(anchorAXRect: CGRect?, panelSize: CGSize) -> NSPoint {
        let screens = NSScreen.screens
        let primaryMaxY = screens.first?.frame.maxY ?? 0

        if let axRect = anchorAXRect, axRect.width >= 0, axRect.height >= 0 {
            // AX coordinates have origin at top-left of primary display.
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

        // Fallback: center near the upper-middle of the screen containing the mouse pointer.
        let mouseLocation = NSEvent.mouseLocation
        let screen = screens.first { $0.frame.contains(mouseLocation) } ?? NSScreen.main ?? screens.first
        let visible = screen?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let x = visible.midX - panelSize.width / 2
        let y = visible.midY - panelSize.height / 2 + 60
        return NSPoint(x: round(x), y: round(y))
    }
}
