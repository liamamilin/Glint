import AppKit
import SwiftUI

@main
struct GlintApp: App {
    @NSApplicationDelegateAdaptor(GlintAppDelegate.self) private var appDelegate
    @StateObject private var coordinator = GlintCoordinator()

    var body: some Scene {
        MenuBarExtra("Glint", systemImage: coordinator.isRouting ? "square.stack.3d.up.fill" : "square.stack.3d.up") {
            VStack(alignment: .leading, spacing: 6) {
                Text("Glint · 常驻语义路由器")
                    .font(.headline)

                Divider()

                // Accessibility status
                if coordinator.isAccessibilityGranted {
                    Label("辅助功能权限：已授权 (\(coordinator.hotkeyPreset.shortLabel) 就绪)", systemImage: "checkmark.circle.fill")
                } else {
                    Label("辅助功能权限：未授权", systemImage: "exclamationmark.triangle.fill")
                    Button("前往系统设置授权…") {
                        coordinator.requestAccessibilityPermission()
                    }
                }

                Menu("呼出快捷键：\(coordinator.hotkeyPreset.shortLabel)") {
                    ForEach(GlintHotkeyPreset.allCases) { preset in
                        Button {
                            coordinator.hotkeyPreset = preset
                        } label: {
                            if preset == coordinator.hotkeyPreset {
                                Text("✓ \(preset.label)")
                            } else {
                                Text(preset.label)
                            }
                        }
                    }
                }

                // Backend status
                Text("路由后端：\(coordinator.backendSummary)")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                Text(coordinator.lastStatusMessage)
                    .font(.caption)
                    .foregroundStyle(.secondary)

                Divider()

                Button("编辑认知卡片库 (cards.yaml)…") {
                    coordinator.openCardsFile()
                }

                Button {
                    coordinator.toggleLaunchAtLogin()
                } label: {
                    Text(coordinator.isLaunchAtLoginEnabled ? "✓ 开机自动启动 Glint" : "开机自动启动 Glint")
                }

                Button("刷新后端与权限状态") {
                    coordinator.refreshPermissionsAndTaps()
                    Task { await coordinator.refreshBackendHealth() }
                }

                Divider()

                Button("退出 Glint") {
                    coordinator.stop()
                    NSApplication.shared.terminate(nil)
                }
                .keyboardShortcut("q")
            }
            .padding(6)
        }
        .menuBarExtraStyle(.menu)
    }
}

final class GlintAppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        // Keep Glint strictly as a menu-bar accessory app with no Dock icon.
        NSApp.setActivationPolicy(.accessory)
    }
}
