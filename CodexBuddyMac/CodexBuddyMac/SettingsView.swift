import SwiftUI
import AppKit

struct SettingsView: View {
    @EnvironmentObject private var poller: UsagePoller
    @AppStorage(AppSettings.bridgeURLKey) private var bridgeURLText = AppSettings.defaultBridgeURLText
    @AppStorage(AppSettings.includeLocalFallbacksKey) private var includeLocalFallbacks = true
    @AppStorage(AppSettings.refreshIntervalKey) private var refreshInterval = AppSettings.defaultRefreshInterval
    @AppStorage(AppSettings.codexLabelKey) private var codexLabel = "CODEX"
    @AppStorage(AppSettings.claudeLabelKey) private var claudeLabel = "CLAUDE"
    @State private var diagnostics: BridgeDiagnostics?
    @State private var setupMessage: String?
    @State private var loginItemEnabled = LoginItemService.isEnabled
    @State private var isBusy = false
    @State private var claudeTokenStatus = "Checking…"

    var body: some View {
        Form {
            Section("Connection") {
                DiagnosticRow(
                    title: "Bridge",
                    value: diagnostics?.bridgeRunning == true ? "Running" : "Stopped",
                    isGood: diagnostics?.bridgeRunning == true
                )

                DiagnosticRow(
                    title: "Bridge files",
                    value: diagnostics?.bridgeInstalled == true ? "Installed" : "Missing",
                    isGood: diagnostics?.bridgeInstalled == true
                )

                LabeledContent("Current URL", value: poller.bridgeURLText)

                if let error = poller.lastError {
                    Text(error)
                        .font(.caption)
                        .foregroundStyle(.orange)
                }

                HStack {
                    Button("Start Bridge") {
                        Task { await startBridge() }
                    }
                    .disabled(isBusy)

                    Button("Restart Bridge") {
                        Task { await restartBridge() }
                    }
                    .disabled(isBusy)

                    Button("Refresh Now") {
                        Task { await refreshUsage() }
                    }
                    .disabled(isBusy)
                }
            }

            Section("Accounts") {
                DiagnosticRow(
                    title: "Codex account",
                    value: diagnostics?.codexAuthFound == true ? "Signed in" : "Not signed in",
                    isGood: diagnostics?.codexAuthFound == true
                )

                DiagnosticRow(
                    title: "Claude",
                    value: diagnostics?.claudeUsageStatus ?? "Checking",
                    isGood: diagnostics?.claudeUsageStatus == "Updated"
                )

                DiagnosticRow(
                    title: "Claude login",
                    value: claudeTokenStatus,
                    isGood: claudeTokenStatus.hasPrefix("Valid")
                )

                Button("Refresh Claude Login") {
                    Task { await refreshClaudeLogin() }
                }
                .disabled(isBusy)
                .help("Renew the Claude OAuth token if usage shows a 401 / login-expired error.")
            }

            Section("Setup") {
                TextField("Bridge URL", text: $bridgeURLText)
                    .textFieldStyle(.roundedBorder)

                Toggle("Try local fallbacks", isOn: $includeLocalFallbacks)

                Stepper(value: $refreshInterval, in: AppSettings.minimumRefreshInterval...600, step: 10) {
                    Text("Refresh every \(Int(refreshInterval)) seconds")
                }

                Toggle("Start CodexBuddy at login", isOn: Binding(
                    get: { loginItemEnabled },
                    set: { enabled in
                        setLoginItem(enabled)
                    }
                ))

                LabeledContent("Login item", value: LoginItemService.statusText)
            }

            Section("Display") {
                TextField("Codex label", text: $codexLabel)
                    .textFieldStyle(.roundedBorder)

                TextField("Claude label", text: $claudeLabel)
                    .textFieldStyle(.roundedBorder)
            }

            Section("Diagnostics") {
                if let diagnostics {
                    LabeledContent("App data", value: diagnostics.appSupportPath)
                    LabeledContent("Logs", value: diagnostics.logsPath)
                }

                if let setupMessage {
                    Text(setupMessage)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                HStack {
                    Button("Run Diagnostics") {
                        Task { await refreshDiagnostics() }
                    }
                    .disabled(isBusy)

                    Button("Open Logs") {
                        openLogs()
                    }
                }
            }
        }
        .onAppear {
            loginItemEnabled = LoginItemService.isEnabled
            Task { await refreshDiagnostics() }
        }
        .onChange(of: bridgeURLText) { poller.applySettings() }
        .onChange(of: includeLocalFallbacks) { poller.applySettings() }
        .onChange(of: refreshInterval) { poller.applySettings() }
        .onChange(of: codexLabel) { poller.applySettings() }
        .onChange(of: claudeLabel) { poller.applySettings() }
        .padding()
        .frame(width: 560)
    }

    private func startBridge() async {
        await runBusyAction("Bridge started.") {
            try BridgeService.startIfNeeded()
            await refreshUsage()
        }
    }

    private func restartBridge() async {
        await runBusyAction("Bridge restarted.") {
            try BridgeService.restart()
            try await Task.sleep(nanoseconds: 800_000_000)
            await refreshUsage()
        }
    }

    private func refreshUsage() async {
        poller.applySettings()
        await poller.refresh()
        await refreshDiagnostics()
    }

    private func refreshDiagnostics() async {
        diagnostics = await BridgeService.diagnostics()
        loginItemEnabled = LoginItemService.isEnabled
        claudeTokenStatus = await BridgeService.claudeTokenStatus()
    }

    private func refreshClaudeLogin() async {
        isBusy = true
        defer { isBusy = false }
        setupMessage = await BridgeService.refreshClaudeLogin()
        await refreshUsage()
    }

    private func setLoginItem(_ enabled: Bool) {
        do {
            try LoginItemService.setEnabled(enabled)
            loginItemEnabled = LoginItemService.isEnabled
            setupMessage = enabled ? "CodexBuddy will start at login." : "CodexBuddy will not start at login."
        } catch {
            loginItemEnabled = LoginItemService.isEnabled
            setupMessage = error.localizedDescription
        }
    }

    private func openLogs() {
        do {
            try BridgeService.installBundledBridge()
            NSWorkspace.shared.open(BridgeService.logsDirectoryURL)
        } catch {
            setupMessage = error.localizedDescription
        }
    }

    private func runBusyAction(_ successMessage: String, action: () async throws -> Void) async {
        isBusy = true
        defer { isBusy = false }

        do {
            try await action()
            setupMessage = successMessage
        } catch {
            setupMessage = error.localizedDescription
        }
    }
}

private struct DiagnosticRow: View {
    let title: String
    let value: String
    let isGood: Bool

    var body: some View {
        LabeledContent {
            Label(value, systemImage: isGood ? "checkmark.circle.fill" : "exclamationmark.circle.fill")
                .foregroundStyle(isGood ? .green : .orange)
        } label: {
            Text(title)
        }
    }
}
