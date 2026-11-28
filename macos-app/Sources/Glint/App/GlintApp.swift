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

                Button {
                    coordinator.seedController.isEnabled.toggle()
                } label: {
                    Text(
                        coordinator.seedController.isEnabled
                            ? "✓ 认知种子轮播 (5min · \(coordinator.seedController.poolMode.shortLabel))"
                            : "开启认知种子轮播 (5min)"
                    )
                }

                Menu("认知种子设置") {
                    Menu("轮播内容池：\(coordinator.seedController.poolMode.shortLabel)") {
                        ForEach(SeedPoolMode.allCases) { mode in
                            Button {
                                coordinator.seedController.poolMode = mode
                            } label: {
                                Text(mode == coordinator.seedController.poolMode ? "✓ \(mode.label)" : mode.label)
                            }
                        }
                    }

                    Button("下一张种子") {
                        coordinator.seedController.next(manual: true)
                    }
                    .disabled(!coordinator.seedController.isEnabled)

                    Menu("静默透明度：\(coordinator.seedController.opacityPreset.label)") {
                        ForEach(SeedOpacityPreset.allCases) { preset in
                            Button {
                                coordinator.seedController.opacityPreset = preset
                            } label: {
                                Text(preset == coordinator.seedController.opacityPreset ? "✓ \(preset.label)" : preset.label)
                            }
                        }
                    }

                    Button("重置小窗到右下角") {
                        coordinator.seedController.resetPosition()
                    }

                    if coordinator.seedController.hiddenCount > 0 {
                        Divider()
                        Button("恢复已隐藏的种子 (\(coordinator.seedController.hiddenCount))") {
                            coordinator.seedController.resetAllHidden()
                        }
                    }
                }

                Divider()

                Button("Glint 管理…") {
                    coordinator.openAdminConsole()
                }

                Button("⚡ 从剪贴板导入认知卡…") {
                    coordinator.openAdminImportFromClipboard()
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
