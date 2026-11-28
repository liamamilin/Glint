import AppKit
import SwiftUI

/// Shared high-contrast frosted glass surface for all Glint floating panels.
/// Layers a dense adaptive charcoal/paper tint over `.regularMaterial` so white text in Dark Mode
/// stays >= 9:1 contrast even over pure white webpages (and Light Mode stays crisp over dark IDEs).
struct GlintGlassBackground: View {
    @Environment(\.colorScheme) private var colorScheme
    var cornerRadius: CGFloat = 14
    var tintOpacity: Double? = nil

    private var resolvedTintOpacity: Double {
        if let tintOpacity { return tintOpacity }
        return colorScheme == .dark ? 0.82 : 0.88
    }

    private var tintColor: Color {
        colorScheme == .dark
            ? Color(red: 0.10, green: 0.11, blue: 0.13)
            : Color(red: 0.98, green: 0.98, blue: 0.99)
    }

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                .fill(.regularMaterial)
            RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                .fill(tintColor.opacity(resolvedTintOpacity))
        }
    }
}

struct GlintGlassBorder: View {
    @Environment(\.colorScheme) private var colorScheme
    var cornerRadius: CGFloat = 14
    var emphasized: Bool = false

    var body: some View {
        RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
            .strokeBorder(
                LinearGradient(
                    colors: colorScheme == .dark
                        ? [
                            Color.white.opacity(emphasized ? 0.28 : 0.20),
                            Color.white.opacity(0.09),
                            Color.white.opacity(0.06)
                        ]
                        : [
                            Color.black.opacity(emphasized ? 0.16 : 0.11),
                            Color.black.opacity(0.09),
                            Color.black.opacity(0.13)
                        ],
                    startPoint: .top,
                    endPoint: .bottom
                ),
                lineWidth: 0.75
            )
    }
}

struct CardPopupView: View {
    let cards: [CardItem]
    let selectedIndex: Int
    let latencyMs: Double
    let appName: String
    let isEditingCustomIntent: Bool
    let onSelect: (Int, Bool) -> Void
    let onOpenCustomInput: () -> Void
    let onSubmitCustomIntent: (String, Bool) -> Void
    let onDismiss: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            // 3 Orthogonal Cognitive Cards + 4th Custom Intent Option
            ForEach(Array(cards.enumerated()), id: \.element.id) { index, card in
                CardRow(
                    index: index,
                    card: card,
                    isSelected: !isEditingCustomIntent && index == selectedIndex
                )
                .contentShape(Rectangle())
                .onTapGesture {
                    let shiftHeld = NSEvent.modifierFlags.contains(.shift)
                    onSelect(index, shiftHeld)
                }
            }

            Divider()
                .opacity(0.25)
                .padding(.horizontal, 6)
                .padding(.vertical, 2)

            if isEditingCustomIntent {
                CustomIntentInputRow(
                    onSubmit: onSubmitCustomIntent,
                    onCancel: onDismiss
                )
            } else {
                CustomIntentOptionRow(
                    isSelected: selectedIndex == cards.count
                )
                .contentShape(Rectangle())
                .onTapGesture {
                    onOpenCustomInput()
                }
            }

            // Quiet footer whisper (still clearly legible over light/dark surfaces)
            HStack(spacing: 8) {
                if isEditingCustomIntent {
                    Text("↵ 记录并追加 · ⇧↵ 仅记录")
                } else {
                    Text("↵ 默认 · ⇧↵ 包裹 · 继续打字自动收起")
                }
                Spacer()
                Button("Esc", action: onDismiss)
                    .buttonStyle(.plain)
            }
            .font(.system(size: 9.5, weight: .regular))
            .foregroundStyle(Color.primary.opacity(0.56))
            .padding(.horizontal, 8)
            .padding(.top, 2)
        }
        .padding(8)
        .frame(width: 380)
        .background(GlintGlassBackground(cornerRadius: 14))
        .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(GlintGlassBorder(cornerRadius: 14, emphasized: true))
    }
}

private struct CardRow: View {
    let index: Int
    let card: CardItem
    let isSelected: Bool

    private var expandedPromptText: String {
        card.prompt
            .replacingOccurrences(of: "{{input}}", with: "「你的输入」")
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .replacingOccurrences(of: "\n+", with: " ", options: .regularExpression)
    }

    private var dimensionDotColor: Color {
        switch (card.primitive ?? card.dimension).lowercased() {
        case "critique": return Color(red: 0.92, green: 0.45, blue: 0.45)
        case "deconstruct", "structure": return Color(red: 0.36, green: 0.68, blue: 0.94)
        case "diverge", "explore": return Color(red: 0.94, green: 0.72, blue: 0.34)
        case "translate", "polish": return Color(red: 0.72, green: 0.56, blue: 0.94)
        case "converge", "engineering": return Color(red: 0.34, green: 0.80, blue: 0.62)
        case "compress": return Color(red: 0.38, green: 0.78, blue: 0.85)
        case "metacognize": return Color(red: 0.96, green: 0.58, blue: 0.36)
        default: return Color.secondary
        }
    }

    private var actionBadgeText: String {
        if card.is_workflow_next == true {
            return "→ 下一步"
        }
        if card.is_l3_breakthrough == true {
            return "✦ L3 发现"
        }
        switch card.action_type {
        case "show_checklist": return "清单·阅读"
        case "open_resource": return "兵器·阅读"
        case "workflow_entry": return "工作流"
        default: return card.mode == .wrap ? "包裹" : "追加"
        }
    }

    var body: some View {
        HStack(alignment: .top, spacing: 9) {
            // Subtle index: quiet text when idle, soft pill when focused
            Text("\(index + 1)")
                .font(.system(size: 10.5, weight: isSelected ? .semibold : .medium, design: .monospaced))
                .frame(width: 18, height: 18)
                .background(
                    isSelected ? Color.primary.opacity(0.14) : Color.clear,
                    in: RoundedRectangle(cornerRadius: 5, style: .continuous)
                )
                .foregroundStyle(isSelected ? Color.primary : Color.primary.opacity(0.35))
                .padding(.top, 1)

            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Circle()
                        .fill(dimensionDotColor.opacity(isSelected ? 0.95 : 0.55))
                        .frame(width: 5, height: 5)

                    Text(card.title)
                        .font(.system(size: 12.5, weight: isSelected ? .semibold : .medium))
                        .foregroundStyle(isSelected ? Color.primary : Color.primary.opacity(0.78))
                        .lineLimit(1)

                    Spacer()

                    if isSelected || card.is_workflow_next == true || card.is_l3_breakthrough == true {
                        Text(card.is_exploration == true ? "✦ \(actionBadgeText)" : actionBadgeText)
                            .font(.system(size: 9.5, weight: .medium))
                            .foregroundStyle(
                                card.is_workflow_next == true
                                    ? Color.accentColor
                                    : (card.is_l3_breakthrough == true
                                        ? Color(red: 0.72, green: 0.56, blue: 0.94)
                                        : Color.primary.opacity(0.42))
                            )
                    }
                }

                Text(isSelected ? expandedPromptText : card.preview)
                    .font(.system(size: 11))
                    .foregroundStyle(isSelected ? Color.primary.opacity(0.76) : Color.primary.opacity(0.46))
                    .lineLimit(isSelected ? 3 : 1)
                    .fixedSize(horizontal: false, vertical: true)
                    .truncationMode(.tail)
            }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 7)
        .background(
            RoundedRectangle(cornerRadius: 9, style: .continuous)
                .fill(isSelected ? Color.primary.opacity(0.085) : Color.clear)
        )
    }
}

private struct CustomIntentOptionRow: View {
    let isSelected: Bool

    var body: some View {
        HStack(alignment: .center, spacing: 9) {
            Text("4")
                .font(.system(size: 10.5, weight: isSelected ? .semibold : .medium, design: .monospaced))
                .frame(width: 18, height: 18)
                .background(
                    isSelected ? Color.primary.opacity(0.14) : Color.clear,
                    in: RoundedRectangle(cornerRadius: 5, style: .continuous)
                )
                .foregroundStyle(isSelected ? Color.primary : Color.primary.opacity(0.35))

            Text("写下我想要的…")
                .font(.system(size: 12, weight: isSelected ? .medium : .regular))
                .foregroundStyle(isSelected ? Color.primary : Color.primary.opacity(0.52))

            Spacer()

            Image(systemName: "square.and.pencil")
                .font(.system(size: 10.5))
                .foregroundStyle(Color.primary.opacity(isSelected ? 0.55 : 0.28))
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 6)
        .background(
            RoundedRectangle(cornerRadius: 9, style: .continuous)
                .fill(isSelected ? Color.primary.opacity(0.085) : Color.clear)
        )
    }
}

private struct CustomIntentInputRow: View {
    let onSubmit: (String, Bool) -> Void
    let onCancel: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "square.and.pencil")
                .font(.system(size: 11.5, weight: .medium))
                .foregroundStyle(Color.primary.opacity(0.6))

            CustomIntentTextField(
                placeholder: "写下你想要的指令或想法…",
                onSubmit: onSubmit,
                onCancel: onCancel
            )
            .frame(height: 20)
        }
        .padding(.horizontal, 9)
        .padding(.vertical, 7)
        .background(
            RoundedRectangle(cornerRadius: 9, style: .continuous)
                .fill(Color.primary.opacity(0.07))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 9, style: .continuous)
                .strokeBorder(Color.primary.opacity(0.18), lineWidth: 0.75)
        )
    }
}

/// AppKit NSTextField bridge that handles Return / Shift+Return / Escape cleanly
/// without conflicting with Chinese/Japanese IME candidate confirmation.
private struct CustomIntentTextField: NSViewRepresentable {
    let placeholder: String
    let onSubmit: (String, Bool) -> Void
    let onCancel: () -> Void

    func makeCoordinator() -> Coordinator {
        Coordinator(onSubmit: onSubmit, onCancel: onCancel)
    }

    func makeNSView(context: Context) -> NSTextField {
        let field = NSTextField()
        field.placeholderString = placeholder
        field.isBordered = false
        field.isBezeled = false
        field.drawsBackground = false
        field.focusRingType = .none
        field.font = .systemFont(ofSize: 12)
        field.lineBreakMode = .byTruncatingTail
        field.delegate = context.coordinator

        DispatchQueue.main.async {
            field.window?.makeFirstResponder(field)
        }
        return field
    }

    func updateNSView(_ nsView: NSTextField, context: Context) {
        context.coordinator.onSubmit = onSubmit
        context.coordinator.onCancel = onCancel
    }

    final class Coordinator: NSObject, NSTextFieldDelegate {
        var onSubmit: (String, Bool) -> Void
        var onCancel: () -> Void

        init(onSubmit: @escaping (String, Bool) -> Void, onCancel: @escaping () -> Void) {
            self.onSubmit = onSubmit
            self.onCancel = onCancel
        }

        func control(_ control: NSControl, textView: NSTextView, doCommandBy commandSelector: Selector) -> Bool {
            if commandSelector == #selector(NSResponder.insertNewline(_:)) {
                let shiftHeld = NSApp.currentEvent?.modifierFlags.contains(.shift) ?? false
                let text = control.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
                if !text.isEmpty {
                    onSubmit(text, !shiftHeld)
                } else {
                    onCancel()
                }
                return true
            }
            if commandSelector == #selector(NSResponder.cancelOperation(_:)) {
                onCancel()
                return true
            }
            return false
        }
    }
}

struct LensReaderView: View {
    let card: CardItem
    let onCopy: () -> Void
    let onInject: () -> Void
    let onClose: () -> Void

    private var cleanBody: String {
        card.prompt.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private var kindLabel: String {
        card.action_type == "open_resource" ? "兵器机制" : "破盲清单"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Text(kindLabel)
                    .font(.system(size: 10, weight: .semibold))
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(Color.accentColor.opacity(0.16), in: Capsule())
                    .foregroundStyle(Color.accentColor)

                Text(card.title)
                    .font(.system(size: 13.5, weight: .semibold))
                    .lineLimit(1)

                Spacer()

                Button(action: onClose) {
                    Image(systemName: "xmark")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(Color.primary.opacity(0.45))
                }
                .buttonStyle(.plain)
            }

            if !card.preview.isEmpty {
                Text(card.preview)
                    .font(.system(size: 11.5))
                    .foregroundStyle(Color.primary.opacity(0.76))
            }

            Divider().opacity(0.28)

            ScrollView {
                Text(cleanBody)
                    .font(.system(size: 12))
                    .foregroundStyle(Color.primary.opacity(0.94))
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .frame(maxHeight: 260)

            Divider().opacity(0.28)

            HStack(spacing: 10) {
                Button("↵ 复制到剪贴板", action: onCopy)
                    .buttonStyle(.plain)
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(Color.accentColor)

                Button("⇧↵ 仍要注入", action: onInject)
                    .buttonStyle(.plain)
                    .font(.system(size: 11))
                    .foregroundStyle(Color.primary.opacity(0.76))

                Spacer()

                Button("Esc 关闭", action: onClose)
                    .buttonStyle(.plain)
                    .font(.system(size: 10.5))
                    .foregroundStyle(Color.primary.opacity(0.62))
            }
        }
        .padding(12)
        .frame(width: 420)
        .background(GlintGlassBackground(cornerRadius: 14))
        .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(GlintGlassBorder(cornerRadius: 14, emphasized: true))
    }
}

struct CardLoadingPopupView: View {
    let appName: String

    var body: some View {
        HStack(spacing: 8) {
            ProgressView()
                .controlSize(.small)
                .scaleEffect(0.75)
            Text("正在为 \(appName) 匹配认知卡片…")
                .font(.system(size: 11.5, weight: .medium))
                .foregroundStyle(Color.primary.opacity(0.78))
                .lineLimit(1)
            Spacer()
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .frame(width: 380)
        .background(GlintGlassBackground(cornerRadius: 14))
        .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(GlintGlassBorder(cornerRadius: 14, emphasized: true))
    }
}

struct GlintToastView: View {
    @Environment(\.colorScheme) private var colorScheme
    let symbol: String
    let message: String

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: symbol)
                .font(.system(size: 10.5, weight: .semibold))
                .foregroundStyle(Color.primary.opacity(0.76))
            Text(message)
                .font(.system(size: 11.5, weight: .medium))
                .foregroundStyle(Color.primary.opacity(0.94))
                .lineLimit(1)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6.5)
        .background(
            ZStack {
                Capsule().fill(.regularMaterial)
                Capsule().fill(
                    (colorScheme == .dark
                        ? Color(red: 0.10, green: 0.11, blue: 0.13)
                        : Color(red: 0.98, green: 0.98, blue: 0.99))
                    .opacity(colorScheme == .dark ? 0.84 : 0.90)
                )
            }
        )
        .overlay(
            Capsule()
                .strokeBorder(
                    colorScheme == .dark ? Color.white.opacity(0.18) : Color.black.opacity(0.12),
                    lineWidth: 0.65
                )
        )
    }
}
