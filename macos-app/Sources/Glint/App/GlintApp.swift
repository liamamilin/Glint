import AppKit
import SwiftUI

@main
struct GlintApp: App {
    @NSApplicationDelegateAdaptor(GlintAppDelegate.self) private var appDelegate
    @StateObject private var coordinator = GlintCoordinator()

    var body: some Scene {
        MenuBarExtra("Glint", systemImage: "sparkles") {
            VStack(alignment: .leading, spacing: 6) {
                Text("Glint · 常驻语义路由器")
                    .font(.headline)

                Divider()

                // Accessibility status
                if coordinator.isAccessibilityGranted {
                    Label("辅助功能权限：已授权 (⌥Space 就绪)", systemImage: "checkmark.circle.fill")
                } else {
                    Label("辅助功能权限：未授权", systemImage: "exclamationmark.triangle.fill")
                    Button("前往系统设置授权…") {
                        coordinator.requestAccessibilityPermission()
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

                Button("刷新后端与权限状态") {
                    coordinator.refreshPermissionsAndTaps()
                    Task { await coordinator.refreshBackendHealth() }
                }

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
