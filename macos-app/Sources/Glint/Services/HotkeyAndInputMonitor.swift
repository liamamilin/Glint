import ApplicationServices
import Carbon
import Foundation

/// High-level keyboard commands intercepted while the Glint card popup is visible.
enum PopupKeyCommand: Sendable {
    case selectIndex(Int)
    case openCustomInput
    case moveUp
    case moveDown
    case confirm(forceWrap: Bool)
    case cancel
    /// User kept typing in the host app: dismiss silently without swallowing the keystroke.
    case dismissAndPassThrough
}

/// Built-in low-conflict hotkey presets persisted in UserDefaults.
enum GlintHotkeyPreset: String, CaseIterable, Identifiable, Sendable {
    case doubleRightOption = "double_right_option"
    case optionSemicolon = "option_semicolon"
    case controlOptionSpace = "ctrl_opt_space"
    case fnSpace = "fn_space"
    case optionSpace = "option_space"

    var id: String { rawValue }

    var label: String {
        switch self {
        case .doubleRightOption: return "双击右 Option (推荐 · 零冲突)"
        case .optionSemicolon: return "Option + ; (⌥;)"
        case .controlOptionSpace: return "Control + Option + Space (⌃⌥Space)"
        case .fnSpace: return "Fn + Space (🌐Space)"
        case .optionSpace: return "Option + Space (⌥Space)"
        }
    }

    var shortLabel: String {
        switch self {
        case .doubleRightOption: return "双击右⌥"
        case .optionSemicolon: return "⌥;"
        case .controlOptionSpace: return "⌃⌥Space"
        case .fnSpace: return "Fn+Space"
        case .optionSpace: return "⌥Space"
        }
    }

    var isDoubleModifierTap: Bool {
        self == .doubleRightOption
    }

    var keyCode: CGKeyCode {
        switch self {
        case .doubleRightOption: return 61 // Right Option (kVK_RightOption)
        case .fnSpace, .controlOptionSpace, .optionSpace: return 49 // Space
        case .optionSemicolon: return 41 // ;
        }
    }

    var modifiers: CGEventFlags {
        switch self {
        case .doubleRightOption: return .maskAlternate
        case .fnSpace: return .maskSecondaryFn
        case .optionSemicolon: return .maskAlternate
        case .controlOptionSpace: return [.maskControl, .maskAlternate]
        case .optionSpace: return .maskAlternate
        }
    }
}

/// Manages two lightweight CGEventTaps (requiring only Accessibility permission, no Input Monitoring):
/// 1. `hotkeyTap`: always-on tap that detects the configured hotkey (default: Double Right Option).
/// 2. `popupTap`: enabled ONLY while the 3-card panel is visible to intercept 1/2/3/↑/↓/Enter/Esc.
@MainActor
final class HotkeyAndInputMonitor {
    var onTriggerHotkey: (@MainActor () -> Void)?
    var onPopupKeyCommand: (@MainActor (PopupKeyCommand) -> Void)?

    // Default hotkey: Double Right Option (zero conflict across macOS apps).
    var preset: GlintHotkeyPreset = .doubleRightOption {
        didSet {
            triggerKeyCode = preset.keyCode
            triggerModifiers = preset.modifiers
            resetDoubleTapState()
        }
    }

    var triggerKeyCode: CGKeyCode = GlintHotkeyPreset.doubleRightOption.keyCode
    var triggerModifiers: CGEventFlags = GlintHotkeyPreset.doubleRightOption.modifiers

    private var activeOptionKeyCode: CGKeyCode?
    private var otherKeyPressedDuringOption: Bool = false
    private var lastCleanOptionReleaseKeyCode: CGKeyCode?
    private var lastCleanOptionReleaseUptime: TimeInterval = 0
    private static let optionKeyCodes: Set<CGKeyCode> = [61] // Right Option only (close to arrow keys & Return)
    private static let doubleTapWindow: TimeInterval = 0.42

    private var hotkeyTapPort: CFMachPort?
    private var hotkeyRunLoopSource: CFRunLoopSource?

    private var popupTapPort: CFMachPort?
    private var popupRunLoopSource: CFRunLoopSource?
    private var isPopupInterceptionEnabled = false

    func start() -> Bool {
        guard AXIsProcessTrusted() else { return false }
        if hotkeyTapPort != nil { return true }

        let hotkeyMask = CGEventMask(
            (1 << CGEventType.keyDown.rawValue) | (1 << CGEventType.flagsChanged.rawValue)
        )
        let popupMask = CGEventMask(1 << CGEventType.keyDown.rawValue)
        let userInfo = Unmanaged.passUnretained(self).toOpaque()

        guard let hotPort = CGEvent.tapCreate(
            tap: .cgSessionEventTap,
            place: .headInsertEventTap,
            options: .defaultTap,
            eventsOfInterest: hotkeyMask,
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
            eventsOfInterest: popupMask,
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

    private func resetDoubleTapState() {
        activeOptionKeyCode = nil
        otherKeyPressedDuringOption = false
        lastCleanOptionReleaseKeyCode = nil
        lastCleanOptionReleaseUptime = 0
    }

    private func handleHotkeyEvent(type: CGEventType, event: CGEvent) -> Unmanaged<CGEvent>? {
        if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
            if let port = hotkeyTapPort {
                CGEvent.tapEnable(tap: port, enable: true)
            }
            return Unmanaged.passUnretained(event)
        }

        // Double Option mode: supports both Left Option (58) and Right Option (61),
        // triggering only on two clean tap-releases of the same Option key within 420ms.
        if preset.isDoubleModifierTap {
            if type == .keyDown {
                if activeOptionKeyCode != nil {
                    otherKeyPressedDuringOption = true
                }
                lastCleanOptionReleaseUptime = 0
                lastCleanOptionReleaseKeyCode = nil
                return Unmanaged.passUnretained(event)
            }
            if type == .flagsChanged {
                let keyCode = CGKeyCode(event.getIntegerValueField(.keyboardEventKeycode))
                let flags = event.flags.intersection([.maskCommand, .maskAlternate, .maskControl, .maskShift])

                if Self.optionKeyCodes.contains(keyCode) {
                    if flags == .maskAlternate {
                        activeOptionKeyCode = keyCode
                        otherKeyPressedDuringOption = false
                    } else if activeOptionKeyCode == keyCode && flags.isEmpty {
                        activeOptionKeyCode = nil
                        if !otherKeyPressedDuringOption {
                            let now = ProcessInfo.processInfo.systemUptime
                            if lastCleanOptionReleaseKeyCode == keyCode
                                && (now - lastCleanOptionReleaseUptime) <= Self.doubleTapWindow {
                                lastCleanOptionReleaseUptime = 0
                                lastCleanOptionReleaseKeyCode = nil
                                DispatchQueue.main.async { [weak self] in
                                    self?.onTriggerHotkey?()
                                }
                            } else {
                                lastCleanOptionReleaseKeyCode = keyCode
                                lastCleanOptionReleaseUptime = now
                            }
                        }
                    } else {
                        // Combined with another modifier; cancel tap sequence.
                        resetDoubleTapState()
                    }
                } else if activeOptionKeyCode != nil {
                    otherKeyPressedDuringOption = true
                }
            }
            return Unmanaged.passUnretained(event)
        }

        guard type == .keyDown else { return Unmanaged.passUnretained(event) }

        let keyCode = CGKeyCode(event.getIntegerValueField(.keyboardEventKeycode))
        let relevantFlags = event.flags.intersection([
            .maskCommand, .maskAlternate, .maskControl, .maskShift, .maskSecondaryFn
        ])

        if keyCode == triggerKeyCode && relevantFlags == triggerModifiers {
            DispatchQueue.main.async { [weak self] in
                self?.onTriggerHotkey?()
            }
            // Swallow the hotkey so the host app does not receive a stray space/character.
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
        case 21 where flags.isEmpty: // 4 -> custom intent input
            command = .openCustomInput
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
            // Type-to-Dismiss: if the user simply resumes typing or deletes text,
            // fade Glint out quietly and let the keystroke pass straight through to the host app.
            let isTypingOrDeleting = flags.isEmpty || flags == .maskShift
            if isTypingOrDeleting {
                DispatchQueue.main.async { [weak self] in
                    self?.onPopupKeyCommand?(.dismissAndPassThrough)
                }
            }
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
