import AppKit
import SwiftUI

private func seedPrimitiveColor(_ primitive: String) -> Color {
    switch primitive.lowercased() {
    case "critique": return Color(red: 0.92, green: 0.45, blue: 0.45)
    case "deconstruct": return Color(red: 0.36, green: 0.68, blue: 0.94)
    case "diverge": return Color(red: 0.94, green: 0.72, blue: 0.34)
    case "translate": return Color(red: 0.72, green: 0.56, blue: 0.94)
    case "converge": return Color(red: 0.34, green: 0.80, blue: 0.62)
    case "compress": return Color(red: 0.38, green: 0.78, blue: 0.85)
    case "metacognize": return Color(red: 0.96, green: 0.58, blue: 0.36)
    default: return Color.secondary
    }
}

private func seedCategoryLabel(_ category: String) -> String {
    switch category.lowercased() {
    case "engineering": return "工程"
    case "product", "product_business": return "产品商业"
    case "thinking", "cognition": return "思维"
    case "writing", "writing_comms": return "写作沟通"
    case "business", "strategy": return "商业"
    case "career", "personal_career": return "个人成长"
    case "design": return "设计"
    case "research": return "研究"
    case "communication": return "沟通"
    case "custom": return "自定义"
    case "general": return "通用"
    default: return category
    }
}

/// Extract unique inline placeholder tokens like `[填入你想讲的概念]`, `【你的主题】`, or `{{topic}}`
/// (excluding `{{input}}` which is handled separately).
private func extractSeedVariables(from prompt: String) -> [String] {
    let pattern = #"(\[[^\[\]\n]{2,28}\]|【[^【】\n]{2,28}】|\{\{(?!input\}\})[^{}\n]{1,24}\}\})"#
    guard let regex = try? NSRegularExpression(pattern: pattern) else { return [] }
    let ns = prompt as NSString
    let matches = regex.matches(in: prompt, range: NSRange(location: 0, length: ns.length))
    var seen = Set<String>()
    var result: [String] = []
    for m in matches {
        let token = ns.substring(with: m.range)
        if !seen.contains(token) {
            seen.insert(token)
            result.append(token)
        }
        if result.count >= 4 { break }
    }
    return result
}

private func cleanPlaceholderLabel(_ token: String) -> String {
    token
        .replacingOccurrences(of: "{{", with: "")
        .replacingOccurrences(of: "}}", with: "")
        .trimmingCharacters(in: CharacterSet(charactersIn: "[]【】 "))
}

/// Semi-transparent, non-intrusive Ambient HUD card for Glint 认知种子.
/// Keeps the glass backdrop lightweight while preserving effortless glanceable text contrast
/// and instant zero-delay inline hover hints on every control.
struct SeedHUDView: View {
    @ObservedObject var controller: SeedController
    @State private var hoveredHint: (text: String, tint: Color)?
    @State private var isHoveringBody: Bool = false

    private var activeBackdropAlpha: Double {
        (controller.isHovering || controller.isReaderOpen) ? 0.88 : controller.opacityPreset.backdropAlpha
    }

    private var activeTextAlpha: Double {
        (controller.isHovering || controller.isReaderOpen) ? 1.0 : controller.opacityPreset.textAlpha
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let seed = controller.currentSeed {
                VStack(alignment: .leading, spacing: 6) {
                    // Header row: shows instant hover hint when pointing at any button, else category & tier
                    HStack(spacing: 6) {
                        if let hint = hoveredHint {
                            Text(hint.text)
                                .font(.system(size: 10.5, weight: .semibold))
                                .foregroundStyle(hint.tint)
                                .lineLimit(1)
                                .transition(.opacity)
                        } else {
                            Circle()
                                .fill(seedPrimitiveColor(seed.primitive).opacity(0.92))
                                .frame(width: 6, height: 6)

                            Text(seedCategoryLabel(seed.category))
                                .font(.system(size: 10.5, weight: .medium))
                                .foregroundStyle(Color.primary.opacity(0.78))

                            if seed.isL3Discovery {
                                Text("✦ L3探索")
                                    .font(.system(size: 9.5, weight: .semibold))
                                    .foregroundStyle(Color(red: 0.78, green: 0.62, blue: 0.98))
                                    .padding(.horizontal, 5)
                                    .padding(.vertical, 1)
                                    .background(
                                        Color(red: 0.72, green: 0.56, blue: 0.94).opacity(0.18),
                                        in: Capsule()
                                    )
                            } else if seed.tier.uppercased() == "S" {
                                Text("S")
                                    .font(.system(size: 9.5, weight: .semibold, design: .monospaced))
                                    .foregroundStyle(Color(red: 0.96, green: 0.76, blue: 0.36))
                                    .padding(.horizontal, 4.5)
                                    .padding(.vertical, 1)
                                    .background(
                                        Color(red: 0.94, green: 0.72, blue: 0.34).opacity(0.18),
                                        in: Capsule()
                                    )
                            } else {
                                Text("·")
                                    .font(.system(size: 10))
                                    .foregroundStyle(Color.primary.opacity(0.48))

                                Text(isHoveringBody ? "点击展开本体 ↗" : "认知种子")
                                    .font(.system(size: 10, weight: isHoveringBody ? .medium : .regular))
                                    .foregroundStyle(isHoveringBody ? Color.accentColor : Color.primary.opacity(0.68))
                            }
                        }

                        Spacer(minLength: 4)

                        if controller.isHovering || controller.isReaderOpen {
                            HStack(spacing: 2) {
                                SeedIconButton(
                                    symbol: "chevron.left",
                                    hint: "上一张",
                                    hoverTint: Color.primary,
                                    onHoverHint: { hoveredHint = $0 }
                                ) {
                                    controller.prev()
                                }
                                .opacity(controller.canGoPrev ? 1.0 : 0.28)
                                .disabled(!controller.canGoPrev)

                                SeedIconButton(
                                    symbol: "chevron.right",
                                    hint: "下一张",
                                    hoverTint: Color.primary,
                                    onHoverHint: { hoveredHint = $0 }
                                ) {
                                    controller.next(manual: true)
                                }

                                SeedIconButton(
                                    symbol: controller.isReaderOpen ? "arrow.down.right.and.arrow.up.left" : "arrow.up.left.and.arrow.down.right",
                                    hint: controller.isReaderOpen ? "收起完整本体" : "展开完整本体",
                                    hoverTint: Color.accentColor,
                                    onHoverHint: { hoveredHint = $0 }
                                ) {
                                    controller.toggleReader()
                                }

                                SeedIconButton(
                                    symbol: "clock.arrow.circlepath",
                                    hint: "7 天内不看",
                                    hoverTint: Color(red: 0.96, green: 0.74, blue: 0.32),
                                    onHoverHint: { hoveredHint = $0 }
                                ) {
                                    controller.snoozeCurrent(days: 7)
                                }

                                SeedIconButton(
                                    symbol: "eye.slash",
                                    hint: "不再轮播（保留快捷键）",
                                    hoverTint: Color(red: 0.95, green: 0.45, blue: 0.45),
                                    onHoverHint: { hoveredHint = $0 }
                                ) {
                                    controller.muteCurrent()
                                }

                                SeedIconButton(
                                    symbol: "xmark",
                                    hint: "暂时隐藏小窗",
                                    hoverTint: Color.primary,
                                    onHoverHint: { hoveredHint = $0 }
                                ) {
                                    controller.isEnabled = false
                                }
                            }
                            .transition(.opacity)
                        } else {
                            Text(controller.remainingFormatted)
                                .font(.system(size: 9.5, weight: .regular, design: .monospaced))
                                .foregroundStyle(Color.primary.opacity(0.45))
                        }
                    }
                    .frame(height: 20)

                    // Title + 1-2 line hook (always crisp & glanceable without hovering)
                    VStack(alignment: .leading, spacing: 3) {
                        Text(seed.title)
                            .font(.system(size: 13.5, weight: .semibold))
                            .foregroundStyle(Color.primary.opacity(0.98 * activeTextAlpha))
                            .lineLimit(1)

                        Text(seed.tagline)
                            .font(.system(size: 11.5, weight: .regular))
                            .foregroundStyle(Color.primary.opacity(0.84 * activeTextAlpha))
                            .lineLimit(2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .id(seed.id)
                    .transition(.opacity)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .contentShape(Rectangle())
                    .onHover { over in
                        isHoveringBody = over
                    }
                    .onTapGesture {
                        controller.toggleReader()
                    }
                }
                .padding(.horizontal, 12)
                .padding(.top, 10)
                .padding(.bottom, 9)

                // Undo whisper bar after mute/snooze (4.5s window to prevent accidental hide)
                if let banner = controller.undoBanner {
                    HStack(spacing: 6) {
                        Text(banner.label)
                            .font(.system(size: 10))
                            .foregroundStyle(Color.primary.opacity(0.68))
                            .lineLimit(1)
                        Spacer()
                        Button("撤销") {
                            controller.undoLastHide()
                        }
                        .buttonStyle(.plain)
                        .font(.system(size: 10.5, weight: .semibold))
                        .foregroundStyle(Color.accentColor)
                    }
                    .padding(.horizontal, 12)
                    .padding(.vertical, 5)
                    .background(Color.primary.opacity(0.06))
                    .transition(.opacity)
                }

                // Hairline 5-minute progress whisper
                GeometryReader { geo in
                    ZStack(alignment: .leading) {
                        Rectangle()
                            .fill(Color.primary.opacity(0.08))
                        Rectangle()
                            .fill(seedPrimitiveColor(seed.primitive).opacity(controller.isHovering ? 0.78 : 0.55))
                            .frame(width: max(0, geo.size.width * controller.progress))
                            .animation(.linear(duration: 1.0), value: controller.progress)
                    }
                }
                .frame(height: 1.5)
            } else {
                // Empty or connecting state
                HStack(spacing: 8) {
                    Image(systemName: "sparkles")
                        .font(.system(size: 11))
                        .foregroundStyle(Color.primary.opacity(0.6))
                    Text(controller.totalL4Count > 0 ? "当前池认知种子已全部隐藏" : "正在连接 Glint 认知种子…")
                        .font(.system(size: 11.5))
                        .foregroundStyle(Color.primary.opacity(0.8))
                    Spacer()
                    if controller.hiddenCount > 0 {
                        Button("恢复") {
                            controller.resetAllHidden()
                        }
                        .buttonStyle(.plain)
                        .font(.system(size: 10.5, weight: .medium))
                        .foregroundStyle(Color.accentColor)
                    }
                    SeedIconButton(
                        symbol: "xmark",
                        hint: "关闭小窗",
                        hoverTint: Color.primary,
                        onHoverHint: { hoveredHint = $0 }
                    ) {
                        controller.isEnabled = false
                    }
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 12)
            }
        }
        .frame(width: 320)
        .background(GlintGlassBackground(cornerRadius: 14, tintOpacity: activeBackdropAlpha))
        .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(GlintGlassBorder(cornerRadius: 14, emphasized: controller.isHovering || controller.isReaderOpen))
        .animation(.easeOut(duration: 0.16), value: controller.isHovering)
        .animation(.easeOut(duration: 0.16), value: controller.isReaderOpen)
        .animation(.easeOut(duration: 0.16), value: controller.opacityPreset)
        .animation(.easeOut(duration: 0.18), value: controller.currentSeed?.id)
        .onHover { hovering in
            controller.setHovering(hovering)
            if !hovering {
                hoveredHint = nil
                isHoveringBody = false
            }
        }
    }
}

private struct SeedIconButton: View {
    let symbol: String
    let hint: String
    let hoverTint: Color
    let onHoverHint: ((text: String, tint: Color)?) -> Void
    let action: () -> Void

    @State private var isOver: Bool = false

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 10.5, weight: .medium))
                .foregroundStyle(isOver ? hoverTint : Color.primary.opacity(0.62))
                .frame(width: 21, height: 21)
                .background(
                    RoundedRectangle(cornerRadius: 5, style: .continuous)
                        .fill(isOver ? hoverTint.opacity(0.16) : Color.clear)
                )
        }
        .buttonStyle(.plain)
        .onHover { over in
            isOver = over
            if over {
                NSCursor.pointingHand.push()
                onHoverHint((text: hint, tint: hoverTint))
            } else {
                NSCursor.pop()
                onHoverHint(nil)
            }
        }
    }
}

/// Enhanced full-card reader anchored cleanly beside the Ambient Seed HUD:
/// supports inline variable fill-in, 1-click copy, Esc to close, and 1-click launch to ChatGPT / Claude / Gemini.
struct SeedReaderView: View {
    @ObservedObject var controller: SeedController
    @State private var variableValues: [String: String] = [:]
    @State private var quickInputText: String = ""

    private func resolvedPrompt(for seed: SeedItem) -> String {
        var text = seed.prompt
        for (token, val) in variableValues {
            let trimmed = val.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty {
                text = text.replacingOccurrences(of: token, with: trimmed)
            }
        }
        let trimmedInput = quickInputText.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.contains("{{input}}") {
            text = text.replacingOccurrences(
                of: "{{input}}",
                with: trimmedInput.isEmpty ? "" : trimmedInput
            )
        } else if !trimmedInput.isEmpty {
            text += "\n\n" + trimmedInput
        }
        return text.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func launchAI(provider: String, label: String, seed: SeedItem) {
        let finalPrompt = resolvedPrompt(for: seed)
        controller.copyPromptText(finalPrompt, feedbackLabel: "已复制并打开 \(label)")
        let encoded = finalPrompt.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? ""
        let urlString: String
        switch provider {
        case "chatgpt":
            urlString = encoded.count <= 3500 ? "https://chatgpt.com/?q=\(encoded)" : "https://chatgpt.com/"
        case "claude":
            urlString = encoded.count <= 3500 ? "https://claude.ai/new?q=\(encoded)" : "https://claude.ai/new"
        default:
            urlString = "https://gemini.google.com/app"
        }
        if let url = URL(string: urlString) {
            NSWorkspace.shared.open(url)
        }
    }

    var body: some View {
        if let seed = controller.currentSeed {
            let vars = extractSeedVariables(from: seed.prompt)
            let hasInputSlot = seed.prompt.contains("{{input}}")

            VStack(alignment: .leading, spacing: 10) {
                // Header
                HStack(spacing: 6) {
                    Circle()
                        .fill(seedPrimitiveColor(seed.primitive))
                        .frame(width: 7, height: 7)

                    Text(seed.isL3Discovery ? "\(seedCategoryLabel(seed.category)) · ✦ L3 休眠探索" : "\(seedCategoryLabel(seed.category)) · L4 Tier \(seed.tier)")
                        .font(.system(size: 10.5, weight: .medium))
                        .foregroundStyle(
                            seed.isL3Discovery
                                ? Color(red: 0.78, green: 0.62, blue: 0.98)
                                : Color.primary.opacity(0.76)
                        )

                    if !seed.source.isEmpty {
                        Text("· \(seed.source)")
                            .font(.system(size: 10))
                            .foregroundStyle(Color.primary.opacity(0.58))
                            .lineLimit(1)
                    }

                    Spacer()

                    if let pct = seed.probability_pct, pct > 0 {
                        Text(String(format: "%.2f%%", pct))
                            .font(.system(size: 9.5, design: .monospaced))
                            .foregroundStyle(Color.primary.opacity(0.62))
                    }

                    Button {
                        controller.closeReader()
                    } label: {
                        Text("Esc")
                            .font(.system(size: 9.5, weight: .medium, design: .monospaced))
                            .foregroundStyle(Color.primary.opacity(0.74))
                            .padding(.horizontal, 6)
                            .padding(.vertical, 2)
                            .background(Color.primary.opacity(0.12), in: RoundedRectangle(cornerRadius: 4, style: .continuous))
                    }
                    .buttonStyle(.plain)
                }

                if let reason = seed.reason_tag, !reason.isEmpty {
                    Text(reason)
                        .font(.system(size: 10.5))
                        .foregroundStyle(Color.primary.opacity(0.68))
                }

                // Title & tagline
                VStack(alignment: .leading, spacing: 3) {
                    Text(seed.title)
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(Color.primary)

                    if !seed.tagline.isEmpty {
                        Text(seed.tagline)
                            .font(.system(size: 11.5))
                            .foregroundStyle(Color.primary.opacity(0.82))
                    }
                }

                // Pain point / blind-spot signal
                if !seed.pain_point.isEmpty && seed.pain_point != seed.tagline {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("适用场景 / 盲区信号")
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(Color.primary.opacity(0.68))
                        Text(seed.pain_point)
                            .font(.system(size: 11.5))
                            .foregroundStyle(Color.primary.opacity(0.94))
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(9)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.primary.opacity(0.07), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                    .overlay(
                        RoundedRectangle(cornerRadius: 8, style: .continuous)
                            .strokeBorder(Color.primary.opacity(0.08), lineWidth: 0.5)
                    )
                }

                // Inline variable fill-in (when prompt contains placeholders or {{input}})
                if !vars.isEmpty || hasInputSlot {
                    VStack(alignment: .leading, spacing: 5) {
                        Text("就地填空（可选，复制/直达 AI 时自动代入）")
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(Color.primary.opacity(0.52))
                        ForEach(vars, id: \.self) { token in
                            TextField(
                                cleanPlaceholderLabel(token),
                                text: Binding(
                                    get: { variableValues[token] ?? "" },
                                    set: { variableValues[token] = $0 }
                                )
                            )
                            .textFieldStyle(.roundedBorder)
                            .font(.system(size: 11))
                        }
                        if hasInputSlot {
                            TextField("补充你的具体问题或上下文（可选）…", text: $quickInputText)
                                .textFieldStyle(.roundedBorder)
                                .font(.system(size: 11))
                        }
                    }
                }

                // Prompt body + Copy + Direct AI launchers
                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        Text("认知指令本体")
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(Color.primary.opacity(0.68))
                        Spacer()
                        Button {
                            controller.copyPromptText(resolvedPrompt(for: seed), feedbackLabel: "已复制指令")
                        } label: {
                            HStack(spacing: 4) {
                                Image(systemName: controller.copiedFeedback ? "checkmark" : "doc.on.doc")
                                    .font(.system(size: 9.5, weight: .semibold))
                                Text(controller.copiedFeedback ? controller.copyFeedbackText : "复制指令")
                                    .font(.system(size: 10.5, weight: .semibold))
                            }
                            .foregroundStyle(controller.copiedFeedback ? Color.green : Color.accentColor)
                        }
                        .buttonStyle(.plain)
                    }

                    ScrollView {
                        Text(
                            resolvedPrompt(for: seed).isEmpty
                                ? seed.prompt.replacingOccurrences(of: "{{input}}", with: "「你的输入」")
                                : resolvedPrompt(for: seed)
                        )
                        .font(.system(size: 11.5))
                        .foregroundStyle(Color.primary.opacity(0.95))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .textSelection(.enabled)
                    }
                    .frame(maxHeight: 144)

                    // Direct AI launch bar
                    HStack(spacing: 6) {
                        Text("直达：")
                            .font(.system(size: 10))
                            .foregroundStyle(Color.primary.opacity(0.66))
                        ForEach([("ChatGPT", "chatgpt"), ("Claude", "claude"), ("Gemini", "gemini")], id: \.1) { label, key in
                            Button {
                                launchAI(provider: key, label: label, seed: seed)
                            } label: {
                                Text("\(label) ↗")
                                    .font(.system(size: 10, weight: .medium))
                                    .padding(.horizontal, 7)
                                    .padding(.vertical, 3)
                                    .background(Color.primary.opacity(0.11), in: RoundedRectangle(cornerRadius: 5, style: .continuous))
                                    .foregroundStyle(Color.primary.opacity(0.92))
                            }
                            .buttonStyle(.plain)
                        }
                        Spacer()
                    }
                }
                .padding(9)
                .background(Color.primary.opacity(0.065), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                .overlay(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .strokeBorder(Color.primary.opacity(0.08), lineWidth: 0.5)
                )

                // Trigger utterances
                if !seed.trigger_utterances.isEmpty {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("下次遇到时，直接这样呼出")
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(Color.primary.opacity(0.68))
                        Text(seed.trigger_utterances.prefix(3).map { "「\($0)」" }.joined(separator: "  "))
                            .font(.system(size: 10.5))
                            .foregroundStyle(Color.primary.opacity(0.82))
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }

                Divider().opacity(0.28)

                // Footer actions
                HStack(spacing: 10) {
                    Button {
                        controller.snoozeCurrent(days: 7)
                    } label: {
                        Label("7 天不看", systemImage: "clock.arrow.circlepath")
                            .font(.system(size: 11, weight: .medium))
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(Color(red: 0.96, green: 0.74, blue: 0.32))

                    Button {
                        controller.muteCurrent()
                    } label: {
                        Label("不再轮播", systemImage: "eye.slash")
                            .font(.system(size: 11, weight: .medium))
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(Color(red: 0.96, green: 0.48, blue: 0.48))

                    Spacer()

                    Button {
                        controller.next(manual: true)
                    } label: {
                        HStack(spacing: 4) {
                            Text("下一张")
                            Image(systemName: "arrow.right")
                        }
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(Color.primary.opacity(0.95))
                        .padding(.horizontal, 10)
                        .padding(.vertical, 5)
                        .background(Color.primary.opacity(0.14), in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(14)
            .frame(width: 400)
            .background(GlintGlassBackground(cornerRadius: 16))
            .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
            .overlay(GlintGlassBorder(cornerRadius: 16, emphasized: true))
            .onChange(of: seed.id) { _, _ in
                variableValues = [:]
                quickInputText = ""
            }
        }
    }
}
