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
        let t0 = CFAbsoluteTimeGetCurrent()
        guard let frontApp = NSWorkspace.shared.frontmostApplication else { return .emptyText }
        let appName = frontApp.localizedName ?? "Unknown"
        let pid = frontApp.processIdentifier

        let lowerApp = appName.lowercased()
        if Self.silentAvoidAppKeywords.contains(where: { lowerApp.contains($0) }) {
            return .silentIgnore
        }

        let didPrime = primeAccessibilityIfNeeded(for: pid)
        if didPrime {
            // Give Chromium/Electron a short window to materialize its accessibility tree on first access.
            try? await Task.sleep(nanoseconds: 60_000_000)
            guard !Task.isCancelled else { return .silentIgnore }
        }
        let focusedElement = focusedAXElement()

        // Privacy redline: never read from password or sensitive fields.
        if let element = focusedElement, isElementSecure(element) {
            NSLog("[Glint] Blocked capture: focused field is sensitive/secure")
            return .blockedSecureField
        }

        let anchorRect = focusedElement.flatMap(resolveCaretOrElementRect)
        let isEditable = focusedElement.map(isElementEditable) ?? true

        func logTier(_ tier: String, chars: Int) {
            let ms = (CFAbsoluteTimeGetCurrent() - t0) * 1000.0
            let line = String(
                format: "[PERF][capture] app=%@ tier=%@ primed=%@ chars=%d ms=%.1f",
                appName, tier, didPrime ? "true" : "false", chars, ms
            )
            NSLog("[Glint] %@", line)
        }

        // Tier 1a: AX selected text (zero side effects).
        if let element = focusedElement,
           let selected = stringAttribute(kAXSelectedTextAttribute as CFString, of: element),
           !selected.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            logTier("1a_ax_selection", chars: selected.count)
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
            logTier("1b_clip_selection", chars: copiedSelection.count)
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
                let (paragraph, wrapPrefix) = extractParagraphAroundCaret(in: fullValue, element: element)
                if !paragraph.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    logTier("2a_ax_paragraph", chars: paragraph.count)
                    return .captured(CapturedContext(
                        appName: appName,
                        appPID: pid,
                        inputText: paragraph,
                        captureScope: .paragraph,
                        anchorRect: anchorRect,
                        isEditable: isEditable,
                        wrapParagraphPrefix: wrapPrefix
                    ))
                }
            } else {
                logTier("3a_ax_full", chars: fullValue.count)
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
            // Editors & browsers: try current paragraph (Cmd + Shift + Up) first so multi-line
            // paragraphs stay intact (and match TextInjector's .paragraph wrap), then fall back
            // to current line (Cmd + Shift + Left) for single-line web inputs. Never Cmd + A.
            if let paragraph = await probeClipboardViaCopy(selectBeforeCopy: .currentParagraph),
               !paragraph.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                logTier("2b_clip_paragraph", chars: paragraph.count)
                return .captured(CapturedContext(
                    appName: appName,
                    appPID: pid,
                    inputText: paragraph,
                    captureScope: .paragraph,
                    anchorRect: anchorRect,
                    isEditable: isEditable
                ))
            }
            guard !Task.isCancelled else { return .silentIgnore }
            if let lineText = await probeClipboardViaCopy(selectBeforeCopy: .currentLine),
               !lineText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                logTier("2b_clip_line", chars: lineText.count)
                return .captured(CapturedContext(
                    appName: appName,
                    appPID: pid,
                    inputText: lineText,
                    captureScope: .line,
                    anchorRect: anchorRect,
                    isEditable: isEditable
                ))
            }
        } else {
            // Standalone chat / input boxes: safe to select field text.
            if let fullText = await probeClipboardViaCopy(selectBeforeCopy: .selectAll),
               !fullText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                logTier("3b_clip_selectall", chars: fullText.count)
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

        logTier("empty", chars: 0)
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
    @discardableResult
    private func primeAccessibilityIfNeeded(for pid: pid_t) -> Bool {
        guard !primedAppPIDs.contains(pid) else { return false }
        primedAppPIDs.insert(pid)
        let appElement = AXUIElementCreateApplication(pid)
        let status = AXUIElementSetAttributeValue(appElement, "AXManualAccessibility" as CFString, kCFBooleanTrue)
        return status == .success
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

    private func extractParagraphAroundCaret(in text: String, element: AXUIElement) -> (String, String?) {
        var rangeValue: CFTypeRef?
        if AXUIElementCopyAttributeValue(element, kAXSelectedTextRangeAttribute as CFString, &rangeValue) == .success,
           let axVal = rangeValue,
           CFGetTypeID(axVal) == AXValueGetTypeID() {
            var cfRange = CFRange(location: 0, length: 0)
            if AXValueGetValue(axVal as! AXValue, .cfRange, &cfRange) {
                let nsText = text as NSString
                let clamped = min(max(cfRange.location, 0), nsText.length)
                let paragraphRange = nsText.paragraphRange(for: NSRange(location: clamped, length: 0))
                let currentPara = nsText.substring(with: paragraphRange)
                if !currentPara.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    let prefixLen = max(0, clamped - paragraphRange.location)
                    let prefix = nsText.substring(with: NSRange(location: paragraphRange.location, length: prefixLen))
                    let suffixLen = max(0, (paragraphRange.location + paragraphRange.length) - clamped)
                    let suffix = nsText.substring(with: NSRange(location: clamped, length: suffixLen))
                    let hasTrailingText = !suffix.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                    return (currentPara, hasTrailingText ? prefix : nil)
                }
                // Caret is sitting on an empty trailing line: scan backward for the nearest non-empty paragraph.
                var cursor = paragraphRange.location
                while cursor > 0 {
                    let prevRange = nsText.paragraphRange(for: NSRange(location: cursor - 1, length: 0))
                    let prevPara = nsText.substring(with: prevRange)
                    if !prevPara.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        return (prevPara, "")
                    }
                    cursor = prevRange.location
                }
            }
        }
        // Fallback: take last non-empty line/paragraph.
        let fallback = text
            .components(separatedBy: .newlines)
            .reversed()
            .first { !$0.trimmingCharacters(in: .whitespaces).isEmpty } ?? text
        return (fallback, nil)
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
        case currentLine
        case currentParagraph
        case selectAll
    }

    private func probeClipboardViaCopy(selectBeforeCopy: SelectionShortcut?) async -> String? {
        guard !Task.isCancelled else { return nil }
        let pasteboard = NSPasteboard.general
        let saved = PasteboardSnapshot.capture(from: pasteboard)
        let sentinelChangeCount = pasteboard.changeCount

        pasteboard.clearContents()
        let clearedChangeCount = pasteboard.changeCount

        if let shortcut = selectBeforeCopy {
            switch shortcut {
            case .currentLine:
                // Cmd + Shift + Left selects from caret to start of current line (works in single-line & multiline inputs)
                postKey(virtualKey: 123, flags: [.maskCommand, .maskShift])
            case .currentParagraph:
                // Cmd + Shift + Up selects from caret to start of current paragraph/block
                postKey(virtualKey: 126, flags: [.maskCommand, .maskShift])
            case .selectAll:
                // Cmd + A
                postKey(virtualKey: 0, flags: .maskCommand)
            }
            try? await Task.sleep(nanoseconds: 25_000_000)
            if Task.isCancelled {
                if pasteboard.changeCount != sentinelChangeCount {
                    PasteboardSnapshot.restore(saved, to: pasteboard)
                }
                return nil
            }
        }

        // Cmd + C
        postKey(virtualKey: 8, flags: .maskCommand)

        // Poll pasteboard at 10ms granularity: empty-selection probe caps at 50ms; synthetic selection caps at 180ms.
        var copied: String?
        let maxPolls = (selectBeforeCopy == nil) ? 5 : 18
        for _ in 0..<maxPolls {
            try? await Task.sleep(nanoseconds: 10_000_000)
            if Task.isCancelled {
                break
            }
            if pasteboard.changeCount != clearedChangeCount {
                copied = pasteboard.string(forType: .string)
                break
            }
        }

        // Collapse temporary selection back to the right edge ONLY if text was actually selected & copied.
        if selectBeforeCopy != nil && copied != nil && !Task.isCancelled {
            postKey(virtualKey: 124, flags: []) // Right arrow
            try? await Task.sleep(nanoseconds: 10_000_000)
        }

        // Restore user's original clipboard in original type order.
        if pasteboard.changeCount != sentinelChangeCount {
            PasteboardSnapshot.restore(saved, to: pasteboard)
        }
        return Task.isCancelled ? nil : copied
    }

    private func postKey(virtualKey: CGKeyCode, flags: CGEventFlags) {
        // Use .privateState so residual Option/modifier flags from the Double-Right-Option hotkey
        // NEVER leak into synthetic Cmd+C / Cmd+Shift+Left events (which would turn Cmd+C into Cmd+Opt+C!).
        let source = CGEventSource(stateID: .privateState)
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
