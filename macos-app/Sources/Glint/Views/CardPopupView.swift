import AppKit
import SwiftUI

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

            // Ultra-quiet footer whisper
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
            .foregroundStyle(Color.primary.opacity(0.38))
            .padding(.horizontal, 8)
            .padding(.top, 2)
        }
        .padding(8)
        .frame(width: 380)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .strokeBorder(
                    LinearGradient(
                        colors: [
                            Color.white.opacity(0.24),
                            Color.primary.opacity(0.08),
                            Color.primary.opacity(0.04)
                        ],
                        startPoint: .top,
                        endPoint: .bottom
                    ),
                    lineWidth: 0.75
                )
        )
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
        switch card.dimension.lowercased() {
        case "critique": return Color(red: 0.92, green: 0.45, blue: 0.45)
        case "structure": return Color(red: 0.36, green: 0.68, blue: 0.94)
        case "explore": return Color(red: 0.94, green: 0.72, blue: 0.34)
        case "polish": return Color(red: 0.72, green: 0.56, blue: 0.94)
        case "engineering": return Color(red: 0.34, green: 0.80, blue: 0.62)
        default: return Color.secondary
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

                    if isSelected {
                        Text(card.mode == .wrap ? "包裹" : "追加")
                            .font(.system(size: 9.5, weight: .medium))
                            .foregroundStyle(Color.primary.opacity(0.42))
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

struct GlintToastView: View {
    let symbol: String
    let message: String

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: symbol)
                .font(.system(size: 10.5, weight: .medium))
                .foregroundStyle(Color.primary.opacity(0.55))
            Text(message)
                .font(.system(size: 11.5, weight: .regular))
                .foregroundStyle(Color.primary.opacity(0.82))
                .lineLimit(1)
        }
        .padding(.horizontal, 11)
        .padding(.vertical, 6)
        .background(.ultraThinMaterial, in: Capsule())
        .overlay(
            Capsule()
                .strokeBorder(Color.primary.opacity(0.10), lineWidth: 0.5)
        )
    }
}
