import SwiftUI

struct CardPopupView: View {
    let cards: [CardItem]
    let selectedIndex: Int
    let latencyMs: Double
    let appName: String
    let onSelect: (Int, Bool) -> Void
    let onDismiss: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            // Header
            HStack(spacing: 6) {
                Image(systemName: "sparkles")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.secondary)
                Text("Glint")
                    .font(.system(size: 12, weight: .semibold))
                Text("· \(appName)")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                Spacer()
                Text("\(Int(latencyMs.rounded())) ms")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.tertiary)
            }
            .padding(.horizontal, 4)

            // 3 Orthogonal Cognitive Cards
            VStack(spacing: 6) {
                ForEach(Array(cards.enumerated()), id: \.element.id) { index, card in
                    CardRow(
                        index: index,
                        card: card,
                        isSelected: index == selectedIndex
                    )
                    .contentShape(Rectangle())
                    .onTapGesture {
                        let shiftHeld = NSEvent.modifierFlags.contains(.shift)
                        onSelect(index, shiftHeld)
                    }
                }
            }

            // Footer shortcut hint
            HStack(spacing: 10) {
                Label("1–3 / ↵ 默认", systemImage: "return")
                Text("⇧↵ 包裹")
                Spacer()
                Button("Esc 取消", action: onDismiss)
                    .buttonStyle(.plain)
            }
            .font(.system(size: 10.5))
            .foregroundStyle(.secondary)
            .padding(.horizontal, 4)
            .padding(.top, 2)
        }
        .padding(12)
        .frame(width: 440)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .strokeBorder(Color.primary.opacity(0.12), lineWidth: 0.75)
        )
    }
}

private struct CardRow: View {
    let index: Int
    let card: CardItem
    let isSelected: Bool

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            // Shortcut badge (1 / 2 / 3)
            Text("\(index + 1)")
                .font(.system(size: 11, weight: .semibold, design: .monospaced))
                .frame(width: 20, height: 20)
                .background(
                    isSelected ? Color.accentColor : Color.primary.opacity(0.08),
                    in: RoundedRectangle(cornerRadius: 6, style: .continuous)
                )
                .foregroundStyle(isSelected ? Color.white : Color.primary)
                .padding(.top, 1)

            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(card.title)
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(.primary)
                        .lineLimit(1)

                    Text(card.dimension)
                        .font(.system(size: 10, weight: .medium))
                        .padding(.horizontal, 6)
                        .padding(.vertical, 1.5)
                        .background(Color.primary.opacity(0.07), in: Capsule())
                        .foregroundStyle(.secondary)

                    Spacer()

                    Text(card.mode == .wrap ? "包裹" : "追加")
                        .font(.system(size: 10))
                        .foregroundStyle(.tertiary)
                }

                Text(card.preview)
                    .font(.system(size: 11.5))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.tail)
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(isSelected ? Color.accentColor.opacity(0.14) : Color.primary.opacity(0.03))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .strokeBorder(
                    isSelected ? Color.accentColor.opacity(0.55) : Color.clear,
                    lineWidth: 1
                )
        )
    }
}
