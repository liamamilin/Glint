import ApplicationServices
import Carbon
import Foundation

/// High-level keyboard commands intercepted while the Glint card popup is visible.
enum PopupKeyCommand: Sendable {
    case selectIndex(Int)
    case moveUp
    case moveDown
    case confirm(forceWrap: Bool)
    case cancel
}

/// Manages two lightweight CGEventTaps (requiring only Accessibility permission, no Input Monitoring):
/// 1. `hotkeyTap`: always-on tap that consumes Option+Space to summon Glint.
/// 2. `popupTap`: enabled ONLY while the 3-card panel is visible to intercept 1/2/3/↑/↓/Enter/Esc.
@MainActor
final class HotkeyAndInputMonitor {
    var onTriggerHotkey: (@MainActor () -> Void)?
    var onPopupKeyCommand: (@MainActor (PopupKeyCommand) -> Void)?

    // Default hotkey: Option + Space (keyCode 49).
    var triggerKeyCode: CGKeyCode = 49
    var triggerModifiers: CGEventFlags = .maskAlternate

    private var hotkeyTapPort: CFMachPort?
    private var hotkeyRunLoopSource: CFRunLoopSource?

    private var popupTapPort: CFMachPort?
    private var popupRunLoopSource: CFRunLoopSource?
    private var isPopupInterceptionEnabled = false

    func start() -> Bool {
        guard AXIsProcessTrusted() else { return false }
        if hotkeyTapPort != nil { return true }

        let mask = CGEventMask(1 << CGEventType.keyDown.rawValue)
        let userInfo = Unmanaged.passUnretained(self).toOpaque()

        guard let hotPort = CGEvent.tapCreate(
            tap: .cgSessionEventTap,
            place: .headInsertEventTap,
            options: .defaultTap,
            eventsOfInterest: mask,
            callback: Self.hotkeyCallback,
            userInfo: userInfo
        ) else {
            NSLog("[Glint] Failed to create global hotkey CGEventTap")
            return false
        }

        let hotSource = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, hotPort, 0)
        if let hotSource {
            CFRunLoopAddSource(CFRunLoopGetMain(), hotSource, .commonModes)
        }
        CGEvent.tapEnable(tap: hotPort, enable: true)
        hotkeyTapPort = hotPort
        hotkeyRunLoopSource = hotSource

        if let popPort = CGEvent.tapCreate(
            tap: .cgSessionEventTap,
            place: .headInsertEventTap,
            options: .defaultTap,
            eventsOfInterest: mask,
            callback: Self.popupCallback,
            userInfo: userInfo
        ) {
            let popSource = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, popPort, 0)
            if let popSource {
                CFRunLoopAddSource(CFRunLoopGetMain(), popSource, .commonModes)
            }
            CGEvent.tapEnable(tap: popPort, enable: false)
            popupTapPort = popPort
            popupRunLoopSource = popSource
        }

        return true
    }

    func stop() {
        if let src = popupRunLoopSource {
            CFRunLoopRemoveSource(CFRunLoopGetMain(), src, .commonModes)
        }
        if let port = popupTapPort {
            CFMachPortInvalidate(port)
        }
        popupRunLoopSource = nil
        popupTapPort = nil
        isPopupInterceptionEnabled = false

        if let src = hotkeyRunLoopSource {
            CFRunLoopRemoveSource(CFRunLoopGetMain(), src, .commonModes)
        }
        if let port = hotkeyTapPort {
            CFMachPortInvalidate(port)
        }
        hotkeyRunLoopSource = nil
        hotkeyTapPort = nil
    }

    func setPopupInterceptionActive(_ active: Bool) {
        isPopupInterceptionEnabled = active
        if let port = popupTapPort {
            CGEvent.tapEnable(tap: port, enable: active)
        }
    }

    private func handleHotkeyEvent(type: CGEventType, event: CGEvent) -> Unmanaged<CGEvent>? {
        if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
            if let port = hotkeyTapPort {
                CGEvent.tapEnable(tap: port, enable: true)
            }
            return Unmanaged.passUnretained(event)
        }
        guard type == .keyDown else { return Unmanaged.passUnretained(event) }

        let keyCode = CGKeyCode(event.getIntegerValueField(.keyboardEventKeycode))
        let relevantFlags = event.flags.intersection([.maskCommand, .maskAlternate, .maskControl, .maskShift])

        if keyCode == triggerKeyCode && relevantFlags == triggerModifiers {
            DispatchQueue.main.async { [weak self] in
                self?.onTriggerHotkey?()
            }
            // Swallow Option+Space so the host app does not receive NBSP.
            return nil
        }
        return Unmanaged.passUnretained(event)
    }

    private func handlePopupEvent(type: CGEventType, event: CGEvent) -> Unmanaged<CGEvent>? {
        if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
            if let port = popupTapPort, isPopupInterceptionEnabled {
                CGEvent.tapEnable(tap: port, enable: true)
            }
            return Unmanaged.passUnretained(event)
        }
        guard isPopupInterceptionEnabled, type == .keyDown else {
            return Unmanaged.passUnretained(event)
        }

        let keyCode = CGKeyCode(event.getIntegerValueField(.keyboardEventKeycode))
        let flags = event.flags.intersection([.maskCommand, .maskAlternate, .maskControl, .maskShift])

        let command: PopupKeyCommand?
        switch keyCode {
        case 18 where flags.isEmpty: // 1
            command = .selectIndex(0)
        case 19 where flags.isEmpty: // 2
            command = .selectIndex(1)
        case 20 where flags.isEmpty: // 3
            command = .selectIndex(2)
        case 126 where flags.isEmpty: // Up Arrow
            command = .moveUp
        case 125 where flags.isEmpty: // Down Arrow
            command = .moveDown
        case 36, 76: // Return / Keypad Enter
            if flags.isEmpty {
                command = .confirm(forceWrap: false)
            } else if flags == .maskShift {
                command = .confirm(forceWrap: true)
            } else {
                command = nil
            }
        case 53 where flags.isEmpty: // Escape
            command = .cancel
        default:
            command = nil
        }

        guard let command else {
            return Unmanaged.passUnretained(event)
        }

        DispatchQueue.main.async { [weak self] in
            self?.onPopupKeyCommand?(command)
        }
        return nil
    }

    private static let hotkeyCallback: CGEventTapCallBack = { _, type, event, refcon in
        guard let refcon else { return Unmanaged.passUnretained(event) }
        let monitor = Unmanaged<HotkeyAndInputMonitor>.fromOpaque(refcon).takeUnretainedValue()
        return MainActor.assumeIsolated {
            monitor.handleHotkeyEvent(type: type, event: event)
        }
    }

    private static let popupCallback: CGEventTapCallBack = { _, type, event, refcon in
        guard let refcon else { return Unmanaged.passUnretained(event) }
        let monitor = Unmanaged<HotkeyAndInputMonitor>.fromOpaque(refcon).takeUnretainedValue()
        return MainActor.assumeIsolated {
            monitor.handlePopupEvent(type: type, event: event)
        }
    }
}
