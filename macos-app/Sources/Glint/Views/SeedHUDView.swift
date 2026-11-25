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
struct SeedHUDView: View {
    @ObservedObject var controller: SeedController

    private var activeOpacity: Double {
        (controller.isHovering || controller.isReaderOpen) ? 0.96 : controller.opacityPreset.idleAlpha
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let seed = controller.currentSeed {
                VStack(alignment: .leading, spacing: 6) {
                    // Header row: primitive dot + category + layer/tier badge + micro action bar on hover
                    HStack(spacing: 6) {
                        Circle()
                            .fill(seedPrimitiveColor(seed.primitive).opacity(controller.isHovering ? 0.95 : 0.75))
                            .frame(width: 6, height: 6)

                        Text(seedCategoryLabel(seed.category))
                            .font(.system(size: 10, weight: .medium))
                            .foregroundStyle(Color.primary.opacity(0.55))

                        if seed.isL3Discovery {
                            Text("✦ L3探索")
                                .font(.system(size: 9, weight: .semibold))
                                .foregroundStyle(Color(red: 0.72, green: 0.56, blue: 0.94))
                                .padding(.horizontal, 4.5)
                                .padding(.vertical, 1)
                                .background(
                                    Color(red: 0.72, green: 0.56, blue: 0.94).opacity(0.14),
                                    in: Capsule()
                                )
                        } else if seed.tier.uppercased() == "S" {
                            Text("S")
                                .font(.system(size: 9, weight: .semibold, design: .monospaced))
                                .foregroundStyle(Color(red: 0.94, green: 0.72, blue: 0.34))
                                .padding(.horizontal, 4)
                                .padding(.vertical, 1)
                                .background(
                                    Color(red: 0.94, green: 0.72, blue: 0.34).opacity(0.14),
                                    in: Capsule()
                                )
                        } else {
                            Text("·")
                                .font(.system(size: 10))
                                .foregroundStyle(Color.primary.opacity(0.28))

                            Text("认知种子")
                                .font(.system(size: 10, weight: .regular))
                                .foregroundStyle(Color.primary.opacity(0.42))
                        }

                        Spacer(minLength: 4)

                        if controller.isHovering || controller.isReaderOpen {
                            HStack(spacing: 2) {
                                SeedIconButton(symbol: "chevron.left", help: "上一张") {
                                    controller.prev()
                                }
                                .opacity(controller.canGoPrev ? 1.0 : 0.3)
                                .disabled(!controller.canGoPrev)
                                SeedIconButton(symbol: "chevron.right", help: "下一张（快速跳过会轻度降权）") {
                                    controller.next(manual: true)
                                }
                                SeedIconButton(
                                    symbol: controller.isReaderOpen ? "arrow.down.right.and.arrow.up.left" : "arrow.up.left.and.arrow.down.right",
                                    help: controller.isReaderOpen ? "收起本体" : "展开本体"
                                ) {
                                    controller.toggleReader()
                                }
                                SeedIconButton(symbol: "clock.arrow.circlepath", help: "7 天不看") {
                                    controller.snoozeCurrent(days: 7)
                                }
                                SeedIconButton(symbol: "eye.slash", help: "不再显示（不影响快捷键召回）") {
                                    controller.muteCurrent()
                                }
                                SeedIconButton(symbol: "xmark", help: "暂时关闭小窗") {
                                    controller.isEnabled = false
                                }
                            }
                            .transition(.opacity)
                        } else {
                            Text(controller.remainingFormatted)
                                .font(.system(size: 9.5, weight: .regular, design: .monospaced))
                                .foregroundStyle(Color.primary.opacity(0.32))
                        }
                    }

                    // Title + 1-2 line hook (clicking expands the full Reader)
                    VStack(alignment: .leading, spacing: 3) {
                        Text(seed.title)
                            .font(.system(size: 13, weight: .semibold))
                            .foregroundStyle(Color.primary.opacity(0.92))
                            .lineLimit(1)

                        Text(seed.tagline)
                            .font(.system(size: 11, weight: .regular))
                            .foregroundStyle(Color.primary.opacity(0.62))
                            .lineLimit(2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .contentShape(Rectangle())
                    .onTapGesture {
                        controller.toggleReader()
                    }
                }
                .padding(.horizontal, 12)
                .padding(.top, 10)
                .padding(.bottom, 9)

                // Hairline 5-minute progress whisper
                GeometryReader { geo in
                    ZStack(alignment: .leading) {
                        Rectangle()
                            .fill(Color.primary.opacity(0.06))
                        Rectangle()
                            .fill(seedPrimitiveColor(seed.primitive).opacity(controller.isHovering ? 0.65 : 0.42))
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
                        .foregroundStyle(Color.primary.opacity(0.45))
                    Text(controller.totalL4Count > 0 ? "当前池认知种子已全部隐藏" : "正在连接 Glint 认知种子…")
                        .font(.system(size: 11.5))
                        .foregroundStyle(Color.primary.opacity(0.6))
                    Spacer()
                    if controller.hiddenCount > 0 {
                        Button("恢复") {
                            controller.resetAllHidden()
                        }
                        .buttonStyle(.plain)
                        .font(.system(size: 10.5, weight: .medium))
                        .foregroundStyle(Color.accentColor)
                    }
                    SeedIconButton(symbol: "xmark", help: "关闭小窗") {
                        controller.isEnabled = false
                    }
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 12)
            }
        }
        .frame(width: 320)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .strokeBorder(
                    LinearGradient(
                        colors: [
                            Color.white.opacity(controller.isHovering ? 0.28 : 0.16),
                            Color.primary.opacity(0.08),
                            Color.primary.opacity(0.04)
                        ],
                        startPoint: .top,
                        endPoint: .bottom
                    ),
                    lineWidth: 0.75
                )
        )
        .opacity(activeOpacity)
        .animation(.easeOut(duration: 0.18), value: controller.isHovering)
        .animation(.easeOut(duration: 0.18), value: controller.isReaderOpen)
        .animation(.easeOut(duration: 0.18), value: controller.opacityPreset)
        .onHover { hovering in
            controller.setHovering(hovering)
        }
    }
}

private struct SeedIconButton: View {
    let symbol: String
    let help: String
    let action: () -> Void

    @State private var isOver: Bool = false

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 10, weight: .medium))
                .foregroundStyle(Color.primary.opacity(isOver ? 0.92 : 0.56))
                .frame(width: 20, height: 20)
                .background(
                    RoundedRectangle(cornerRadius: 5, style: .continuous)
                        .fill(isOver ? Color.primary.opacity(0.12) : Color.clear)
                )
        }
        .buttonStyle(.plain)
        .help(help)
        .onHover { isOver = $0 }
    }
}

/// Enhanced full-card reader anchored cleanly beside the Ambient Seed HUD:
/// supports inline variable fill-in, 1-click copy, and 1-click launch to ChatGPT / Claude / Gemini.
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

    private func launchAI(provider: String, seed: SeedItem) {
        let finalPrompt = resolvedPrompt(for: seed)
        controller.copyPromptText(finalPrompt)
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
                                ? Color(red: 0.72, green: 0.56, blue: 0.94)
                                : Color.primary.opacity(0.58)
                        )

                    if !seed.source.isEmpty {
                        Text("· \(seed.source)")
                            .font(.system(size: 10))
                            .foregroundStyle(Color.primary.opacity(0.38))
                            .lineLimit(1)
                    }

                    Spacer()

                    if let pct = seed.probability_pct, pct > 0 {
                        Text(String(format: "%.2f%%", pct))
                            .font(.system(size: 9.5, design: .monospaced))
                            .foregroundStyle(Color.primary.opacity(0.38))
                            .help(seed.reason_tag ?? "当前池动态抽样概率")
                    }

                    SeedIconButton(symbol: "xmark", help: "收起") {
                        controller.closeReader()
                    }
                }

                if let reason = seed.reason_tag, !reason.isEmpty {
                    Text(reason)
                        .font(.system(size: 10))
                        .foregroundStyle(Color.primary.opacity(0.45))
                }

                // Title & tagline
                VStack(alignment: .leading, spacing: 3) {
                    Text(seed.title)
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(Color.primary)

                    if !seed.tagline.isEmpty {
                        Text(seed.tagline)
                            .font(.system(size: 11.5))
                            .foregroundStyle(Color.primary.opacity(0.65))
                    }
                }

                // Pain point / blind-spot signal
                if !seed.pain_point.isEmpty && seed.pain_point != seed.tagline {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("适用场景 / 盲区信号")
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(Color.primary.opacity(0.48))
                        Text(seed.pain_point)
                            .font(.system(size: 11.5))
                            .foregroundStyle(Color.primary.opacity(0.84))
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(9)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                }

                // Inline variable fill-in (when prompt contains placeholders or {{input}})
                if !vars.isEmpty || hasInputSlot {
                    VStack(alignment: .leading, spacing: 5) {
                        Text("就地填空（可选，复制/直达 AI 时自动代入）")
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(Color.primary.opacity(0.48))
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
                            .foregroundStyle(Color.primary.opacity(0.48))
                        Spacer()
                        Button {
                            controller.copyPromptText(resolvedPrompt(for: seed))
                        } label: {
                            HStack(spacing: 4) {
                                Image(systemName: controller.copiedFeedback ? "checkmark" : "doc.on.doc")
                                    .font(.system(size: 9.5, weight: .medium))
                                Text(controller.copiedFeedback ? "已复制" : "复制指令")
                                    .font(.system(size: 10, weight: .medium))
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
                        .foregroundStyle(Color.primary.opacity(0.86))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .textSelection(.enabled)
                    }
                    .frame(maxHeight: 144)

                    // Direct AI launch bar
                    HStack(spacing: 6) {
                        Text("直达：")
                            .font(.system(size: 10))
                            .foregroundStyle(Color.primary.opacity(0.42))
                        ForEach([("ChatGPT", "chatgpt"), ("Claude", "claude"), ("Gemini", "gemini")], id: \.1) { label, key in
                            Button {
                                launchAI(provider: key, seed: seed)
                            } label: {
                                Text("\(label) ↗")
                                    .font(.system(size: 10, weight: .medium))
                                    .padding(.horizontal, 7)
                                    .padding(.vertical, 3)
                                    .background(Color.primary.opacity(0.08), in: RoundedRectangle(cornerRadius: 5, style: .continuous))
                                    .foregroundStyle(Color.primary.opacity(0.82))
                            }
                            .buttonStyle(.plain)
                            .help("复制指令并在 \(label) 中打开")
                        }
                        Spacer()
                    }
                }
                .padding(9)
                .background(Color.primary.opacity(0.045), in: RoundedRectangle(cornerRadius: 8, style: .continuous))

                // Trigger utterances
                if !seed.trigger_utterances.isEmpty {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("下次遇到时，直接这样呼出")
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(Color.primary.opacity(0.48))
                        Text(seed.trigger_utterances.prefix(3).map { "「\($0)」" }.joined(separator: "  "))
                            .font(.system(size: 10.5))
                            .foregroundStyle(Color.primary.opacity(0.64))
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }

                Divider().opacity(0.22)

                // Footer actions
                HStack(spacing: 8) {
                    Button {
                        controller.snoozeCurrent(days: 7)
                    } label: {
                        Label("7 天不看", systemImage: "clock.arrow.circlepath")
                            .font(.system(size: 11, weight: .medium))
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(Color.primary.opacity(0.62))

                    Button {
                        controller.muteCurrent()
                    } label: {
                        Label("不再轮播", systemImage: "eye.slash")
                            .font(.system(size: 11, weight: .medium))
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(Color.red.opacity(0.78))

                    Spacer()

                    Button {
                        controller.next(manual: true)
                    } label: {
                        HStack(spacing: 4) {
                            Text("下一张")
                            Image(systemName: "arrow.right")
                        }
                        .font(.system(size: 11, weight: .semibold))
                        .padding(.horizontal, 10)
                        .padding(.vertical, 5)
                        .background(Color.primary.opacity(0.1), in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(14)
            .frame(width: 400)
            .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .strokeBorder(Color.white.opacity(0.2), lineWidth: 0.75)
            )
            .onChange(of: seed.id) { _, _ in
                variableValues = [:]
                quickInputText = ""
            }
        }
    }
}
