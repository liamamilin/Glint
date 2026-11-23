import AppKit
import ApplicationServices
import Foundation

enum ContextCaptureOutcome: Sendable {
    case captured(CapturedContext)
    case blockedSecureField
    case silentIgnore
    case emptyText
}

/// Reads frontmost app name, caret anchor geometry, and input text using a safe 3-tier fallback:
/// 1. Selected text (AX first, clipboard probe fallback)
/// 2. Current paragraph only for document/code editors (never Cmd+A in editors!)
/// 3. Full field text only for chat/standalone input apps
/// Enforces SecureFieldDetector before reading any field text.
@MainActor
final class ContextReader {
    /// Apps where we NEVER issue Cmd+A (document editors, code editors, and web browsers
    /// where users may be editing Notion / Feishu Docs / Google Docs / Yuque).
    private static let conservativeAppKeywords: [String] = [
        "obsidian", "cursor", "code", "xcode", "notion", "notes", "备忘录",
        "typora", "bear", "craft", "pages", "word", "sublime", "zed", "nova", "textedit",
        "chrome", "safari", "arc", "firefox", "edge", "brave", "opera", "vivaldi", "dia", "zen"
    ]

    /// Apps where Glint stays completely invisible even if triggered accidentally.
    private static let silentAvoidAppKeywords: [String] = [
        "1password", "bitwarden", "keepass", "keychain", "钥匙串", "passwords", "密码", "loginwindow"
    ]

    private var primedAppPIDs: Set<pid_t> = []

    func captureContext() async -> ContextCaptureOutcome {
        guard let frontApp = NSWorkspace.shared.frontmostApplication else { return .emptyText }
        let appName = frontApp.localizedName ?? "Unknown"
        let pid = frontApp.processIdentifier

        let lowerApp = appName.lowercased()
        if Self.silentAvoidAppKeywords.contains(where: { lowerApp.contains($0) }) {
            return .silentIgnore
        }

        primeAccessibilityIfNeeded(for: pid)
        let focusedElement = focusedAXElement()

        // Privacy redline: never read from password or sensitive fields.
        if let element = focusedElement, isElementSecure(element) {
            NSLog("[Glint] Blocked capture: focused field is sensitive/secure")
            return .blockedSecureField
        }

        let anchorRect = focusedElement.flatMap(resolveCaretOrElementRect)
        let isEditable = focusedElement.map(isElementEditable) ?? true

        // Tier 1a: AX selected text (zero side effects).
        if let element = focusedElement,
           let selected = stringAttribute(kAXSelectedTextAttribute as CFString, of: element),
           !selected.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return .captured(CapturedContext(
                appName: appName,
                appPID: pid,
                inputText: selected,
                captureScope: .selection,
                anchorRect: anchorRect,
                isEditable: isEditable
            ))
        }

        // Tier 1b: Only probe selection via Cmd+C if AX couldn't prove the selection is empty.
        // Skipping Cmd+C when selection length is 0 saves ~100ms and eliminates macOS beep / menu flash.
        let axSelectionLength = focusedElement.flatMap(selectedRangeLength)
        let shouldProbeSelectionViaClipboard = (axSelectionLength == nil) || (axSelectionLength! > 0)

        if shouldProbeSelectionViaClipboard,
           let copiedSelection = await probeClipboardViaCopy(selectBeforeCopy: nil),
           !copiedSelection.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return .captured(CapturedContext(
                appName: appName,
                appPID: pid,
                inputText: copiedSelection,
                captureScope: .selection,
                anchorRect: anchorRect,
                isEditable: isEditable
            ))
        }

        let isConservative = isConservativeApp(appName)

        // Tier 2 / Tier 3a: Try reading AXValue directly (zero side effects).
        if let element = focusedElement,
           let fullValue = stringAttribute(kAXValueAttribute as CFString, of: element),
           !fullValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            if isConservative {
                let paragraph = extractParagraphAroundCaret(in: fullValue, element: element)
                if !paragraph.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    return .captured(CapturedContext(
                        appName: appName,
                        appPID: pid,
                        inputText: paragraph,
                        captureScope: .paragraph,
                        anchorRect: anchorRect,
                        isEditable: isEditable
                    ))
                }
            } else {
                return .captured(CapturedContext(
                    appName: appName,
                    appPID: pid,
                    inputText: fullValue,
                    captureScope: .fullField,
                    anchorRect: anchorRect,
                    isEditable: isEditable
                ))
            }
        }

        // Tier 2 / Tier 3b: Safe synthetic selection + copy fallback for Electron/web composers.
        if isConservative {
            // Editors & browsers: ONLY select current paragraph (Cmd + Shift + Up), never Cmd + A.
            if let paragraph = await probeClipboardViaCopy(selectBeforeCopy: .currentParagraph),
               !paragraph.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                return .captured(CapturedContext(
                    appName: appName,
                    appPID: pid,
                    inputText: paragraph,
                    captureScope: .paragraph,
                    anchorRect: anchorRect,
                    isEditable: isEditable
                ))
            }
        } else {
            // Standalone chat / input boxes: safe to select field text.
            if let fullText = await probeClipboardViaCopy(selectBeforeCopy: .selectAll),
               !fullText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                return .captured(CapturedContext(
                    appName: appName,
                    appPID: pid,
                    inputText: fullText,
                    captureScope: .fullField,
                    anchorRect: anchorRect,
                    isEditable: isEditable
                ))
            }
        }

        return .emptyText
    }

    private func isConservativeApp(_ appName: String) -> Bool {
        let lower = appName.lowercased()
        return Self.conservativeAppKeywords.contains { lower.contains($0) }
    }

    private func isElementEditable(_ element: AXUIElement) -> Bool {
        let role = stringAttribute(kAXRoleAttribute as CFString, of: element) ?? ""
        let editableRoles: Set<String> = [
            kAXTextFieldRole as String,
            kAXTextAreaRole as String,
            kAXComboBoxRole as String,
            "AXSearchField"
        ]
        if editableRoles.contains(role) {
            return true
        }
        // For web/Electron surfaces, check whether AXValue is settable or AXEditable is true.
        var settable: DarwinBoolean = false
        if AXUIElementIsAttributeSettable(element, kAXValueAttribute as CFString, &settable) == .success,
           settable.boolValue {
            return true
        }
        var editableRef: CFTypeRef?
        if AXUIElementCopyAttributeValue(element, "AXEditable" as CFString, &editableRef) == .success,
           let flag = editableRef as? Bool {
            return flag
        }
        // Static read-only elements (PDF, web articles, terminal output) return false.
        let readOnlyRoles: Set<String> = [
            kAXStaticTextRole as String,
            "AXHeading",
            "AXLink",
            "AXImage",
            "AXGroup"
        ]
        if readOnlyRoles.contains(role) {
            return false
        }
        // Default to true when uncertain (e.g. custom Electron composers).
        return true
    }

    // MARK: - Accessibility helpers

    /// Enables Chromium/Electron accessibility tree lazily once per process PID.
    private func primeAccessibilityIfNeeded(for pid: pid_t) {
        guard !primedAppPIDs.contains(pid) else { return }
        primedAppPIDs.insert(pid)
        let appElement = AXUIElementCreateApplication(pid)
        AXUIElementSetAttributeValue(appElement, "AXManualAccessibility" as CFString, kCFBooleanTrue)
    }

    private func selectedRangeLength(of element: AXUIElement) -> Int? {
        var rangeValue: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXSelectedTextRangeAttribute as CFString, &rangeValue) == .success,
              let axVal = rangeValue,
              CFGetTypeID(axVal) == AXValueGetTypeID() else {
            return nil
        }
        var cfRange = CFRange(location: 0, length: 0)
        guard AXValueGetValue(axVal as! AXValue, .cfRange, &cfRange) else { return nil }
        return cfRange.length
    }

    private func focusedAXElement() -> AXUIElement? {
        let systemWide = AXUIElementCreateSystemWide()
        var value: CFTypeRef?
        let status = AXUIElementCopyAttributeValue(systemWide, kAXFocusedUIElementAttribute as CFString, &value)
        guard status == .success, let value else { return nil }
        return (value as! AXUIElement)
    }

    private func isElementSecure(_ element: AXUIElement) -> Bool {
        let role = stringAttribute(kAXRoleAttribute as CFString, of: element)
        let subrole = stringAttribute(kAXSubroleAttribute as CFString, of: element)
        let roleDesc = stringAttribute(kAXRoleDescriptionAttribute as CFString, of: element)
        let title = stringAttribute(kAXTitleAttribute as CFString, of: element)
        let desc = stringAttribute(kAXDescriptionAttribute as CFString, of: element)
        return SecureFieldDetector.isSecure(
            role: role,
            subrole: subrole,
            roleDescription: roleDesc,
            title: title,
            descriptionLabel: desc
        )
    }

    private func stringAttribute(_ attr: CFString, of element: AXUIElement) -> String? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, attr, &value) == .success else { return nil }
        return value as? String
    }

    private func extractParagraphAroundCaret(in text: String, element: AXUIElement) -> String {
        var rangeValue: CFTypeRef?
        if AXUIElementCopyAttributeValue(element, kAXSelectedTextRangeAttribute as CFString, &rangeValue) == .success,
           let axVal = rangeValue,
           CFGetTypeID(axVal) == AXValueGetTypeID() {
            var cfRange = CFRange(location: 0, length: 0)
            if AXValueGetValue(axVal as! AXValue, .cfRange, &cfRange) {
                let nsText = text as NSString
                let clamped = min(max(cfRange.location, 0), nsText.length)
                let paragraphRange = nsText.paragraphRange(for: NSRange(location: clamped, length: 0))
                return nsText.substring(with: paragraphRange)
            }
        }
        // Fallback: take last non-empty line/paragraph.
        return text
            .components(separatedBy: .newlines)
            .reversed()
            .first { !$0.trimmingCharacters(in: .whitespaces).isEmpty } ?? text
    }

    private func resolveCaretOrElementRect(_ element: AXUIElement) -> CGRect? {
        var rangeValue: CFTypeRef?
        if AXUIElementCopyAttributeValue(element, kAXSelectedTextRangeAttribute as CFString, &rangeValue) == .success,
           let axRange = rangeValue {
            var boundsValue: CFTypeRef?
            let status = AXUIElementCopyParameterizedAttributeValue(
                element,
                kAXBoundsForRangeParameterizedAttribute as CFString,
                axRange,
                &boundsValue
            )
            if status == .success, let boundsValue, CFGetTypeID(boundsValue) == AXValueGetTypeID() {
                var rect = CGRect.zero
                if AXValueGetValue(boundsValue as! AXValue, .cgRect, &rect), rect.size != .zero {
                    return rect
                }
            }
        }

        // Fallback to element frame.
        var posRef: CFTypeRef?
        var sizeRef: CFTypeRef?
        if AXUIElementCopyAttributeValue(element, kAXPositionAttribute as CFString, &posRef) == .success,
           AXUIElementCopyAttributeValue(element, kAXSizeAttribute as CFString, &sizeRef) == .success,
           let posRef, let sizeRef {
            var point = CGPoint.zero
            var size = CGSize.zero
            if AXValueGetValue(posRef as! AXValue, .cgPoint, &point),
               AXValueGetValue(sizeRef as! AXValue, .cgSize, &size),
               size.width > 0, size.height > 0 {
                return CGRect(origin: point, size: size)
            }
        }
        return nil
    }

    // MARK: - Clipboard fallback with full pasteboard preservation

    private enum SelectionShortcut {
        case currentParagraph
        case selectAll
    }

    private func probeClipboardViaCopy(selectBeforeCopy: SelectionShortcut?) async -> String? {
        let pasteboard = NSPasteboard.general
        let saved = PasteboardSnapshot.capture(from: pasteboard)
        let sentinelChangeCount = pasteboard.changeCount

        pasteboard.clearContents()
        let clearedChangeCount = pasteboard.changeCount

        if let shortcut = selectBeforeCopy {
            switch shortcut {
            case .currentParagraph:
                // Cmd + Shift + Up selects from caret to start of current paragraph/line
                postKey(virtualKey: 126, flags: [.maskCommand, .maskShift])
            case .selectAll:
                // Cmd + A
                postKey(virtualKey: 0, flags: .maskCommand)
            }
            try? await Task.sleep(nanoseconds: 45_000_000)
        }

        // Cmd + C
        postKey(virtualKey: 8, flags: .maskCommand)
        try? await Task.sleep(nanoseconds: 90_000_000)

        let copied: String? = (pasteboard.changeCount != clearedChangeCount)
            ? pasteboard.string(forType: .string)
            : nil

        // Collapse temporary selection back to the right edge so we don't leave text highlighted.
        if selectBeforeCopy != nil {
            postKey(virtualKey: 124, flags: []) // Right arrow
            try? await Task.sleep(nanoseconds: 25_000_000)
        }

        // Restore user's original clipboard in original type order.
        if pasteboard.changeCount != sentinelChangeCount {
            PasteboardSnapshot.restore(saved, to: pasteboard)
        }
        return copied
    }

    private func postKey(virtualKey: CGKeyCode, flags: CGEventFlags) {
        let source = CGEventSource(stateID: .combinedSessionState)
        guard let down = CGEvent(keyboardEventSource: source, virtualKey: virtualKey, keyDown: true),
              let up = CGEvent(keyboardEventSource: source, virtualKey: virtualKey, keyDown: false) else {
            return
        }
        down.flags = flags
        up.flags = flags
        down.post(tap: .cgAnnotatedSessionEventTap)
        up.post(tap: .cgAnnotatedSessionEventTap)
    }
}
