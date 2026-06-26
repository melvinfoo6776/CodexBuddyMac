import Foundation
import Darwin
import CryptoKit

struct BridgeDiagnostics {
    let bridgeInstalled: Bool
    let bridgeRunning: Bool
    let codexAuthFound: Bool
    let claudeUsageStatus: String
    let appSupportPath: String
    let logsPath: String
}

struct ClaudeLoginRefreshResult {
    let succeeded: Bool
    let message: String
    let restartRecommended: Bool
}

struct ClaudeTokenDetails {
    let status: String
    let automaticRefresh: String
}

private struct BridgeHealth: Decodable {
    let status: String
    let version: String
    let buildID: String
    let pid: Int32
    let instanceID: String

    enum CodingKeys: String, CodingKey {
        case status, version, pid
        case buildID = "build_id"
        case instanceID = "instance_id"
    }
}

enum BridgeService {
    static let bridgeURL = URL(string: "http://127.0.0.1:8789/usage.json")!

    private static let bundledBridgeFiles = [
        "codex_usage_server.py",
        "usage.json",
        "claude_usage.json"
    ]

    private static var process: Process?
    private static var stdoutHandle: FileHandle?
    private static var stderrHandle: FileHandle?
    private static var startAttempted = false
    private static var ownsBridgeProcess = false

    static var appSupportURL: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return base.appendingPathComponent("CodexBuddy", isDirectory: true)
    }

    static var bridgeDirectoryURL: URL {
        appSupportURL.appendingPathComponent("Bridge", isDirectory: true)
    }

    static var logsDirectoryURL: URL {
        appSupportURL.appendingPathComponent("Logs", isDirectory: true)
    }

    private static var scriptURL: URL {
        bridgeDirectoryURL.appendingPathComponent("codex_usage_server.py")
    }

    private static var fallbackUsageURL: URL {
        bridgeDirectoryURL.appendingPathComponent("usage.json")
    }

    private static var claudeUsageURL: URL {
        bridgeDirectoryURL.appendingPathComponent("claude_usage.json")
    }

    static func installBundledBridge() throws {
        let fileManager = FileManager.default
        try fileManager.createDirectory(at: bridgeDirectoryURL, withIntermediateDirectories: true)
        try fileManager.createDirectory(at: logsDirectoryURL, withIntermediateDirectories: true)

        for fileName in bundledBridgeFiles {
            guard let bundledURL = bundledResourceURL(named: fileName) else {
                throw BridgeServiceError.missingBundledFile(fileName)
            }

            let destinationURL = bridgeDirectoryURL.appendingPathComponent(fileName)
            let exists = fileManager.fileExists(atPath: destinationURL.path)
            // The bundle is the source of truth for the script, but only rewrite
            // the installed copy when it is missing or actually differs. Copying
            // unconditionally on every refresh was pointless disk churn and made
            // it look like fixes "never ran" when only the installed copy was
            // hand-edited.
            let needsCopy: Bool
            if !exists {
                needsCopy = true
            } else if fileName == "codex_usage_server.py" {
                needsCopy = !fileManager.contentsEqual(atPath: bundledURL.path, andPath: destinationURL.path)
            } else {
                needsCopy = false
            }
            if needsCopy {
                if exists {
                    try fileManager.removeItem(at: destinationURL)
                }
                try fileManager.copyItem(at: bundledURL, to: destinationURL)
            }
        }

        try fileManager.setAttributes([.posixPermissions: 0o755], ofItemAtPath: scriptURL.path)
    }

    private static func bundledResourceURL(named fileName: String) -> URL? {
        let resourceName = (fileName as NSString).deletingPathExtension
        let resourceExtension = (fileName as NSString).pathExtension

        return Bundle.main.url(
            forResource: resourceName,
            withExtension: resourceExtension,
            subdirectory: "Bridge"
        ) ?? Bundle.main.url(
            forResource: resourceName,
            withExtension: resourceExtension
        )
    }

    static func startIfNeeded() async throws {
        try installBundledBridge()
        let expectedBuildID = try installedBridgeBuildID()

        if let health = await bridgeHealth(),
           health.status == "ok",
           health.buildID == expectedBuildID {
            ownsBridgeProcess = process?.processIdentifier == health.pid
            startAttempted = true
            return
        }

        // Async callers can overlap while the first launch is waiting for
        // health. Join that launch instead of terminating and replacing it.
        if startAttempted, let runningProcess = process, runningProcess.isRunning {
            guard await waitForBridgeHealth(
                expectedBuildID: expectedBuildID,
                expectedPID: runningProcess.processIdentifier
            ) != nil else {
                throw BridgeServiceError.healthCheckFailed
            }
            return
        }

        stopOwnedBridge()
        reclaimOrphanBridge()
        startAttempted = true

        let stdoutURL = logsDirectoryURL.appendingPathComponent("usage-server.log")
        let stderrURL = logsDirectoryURL.appendingPathComponent("usage-server.err.log")
        FileManager.default.createFile(atPath: stdoutURL.path, contents: nil)
        FileManager.default.createFile(atPath: stderrURL.path, contents: nil)

        let bridge = Process()
        bridge.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        bridge.currentDirectoryURL = bridgeDirectoryURL
        bridge.arguments = [
            scriptURL.path,
            "--host", "127.0.0.1",
            "--port", "8789",
            "--file", fallbackUsageURL.path,
            "--claude-file", claudeUsageURL.path,
            "--no-discovery"
        ]
        stdoutHandle = try FileHandle(forWritingTo: stdoutURL)
        stderrHandle = try FileHandle(forWritingTo: stderrURL)
        bridge.standardOutput = stdoutHandle
        bridge.standardError = stderrHandle
        let launchedStdoutHandle = stdoutHandle
        let launchedStderrHandle = stderrHandle
        bridge.terminationHandler = { _ in
            try? launchedStdoutHandle?.close()
            try? launchedStderrHandle?.close()
        }

        do {
            try bridge.run()
            process = bridge
            ownsBridgeProcess = true
        } catch {
            closeLogHandles()
            startAttempted = false
            ownsBridgeProcess = false
            throw error
        }

        guard let health = await waitForBridgeHealth(
            expectedBuildID: expectedBuildID,
            expectedPID: bridge.processIdentifier
        ) else {
            stopOwnedBridge()
            throw BridgeServiceError.healthCheckFailed
        }
        guard health.instanceID.isEmpty == false else {
            stopOwnedBridge()
            throw BridgeServiceError.healthCheckFailed
        }
    }

    static func restart() async throws {
        stopOwnedBridge()
        reclaimOrphanBridge()
        try await startIfNeeded()
    }

    /// App termination must only stop a process launched by this app instance.
    /// An adopted bridge may still be serving another running app instance.
    static func stop() {
        stopOwnedBridge()
    }

    private static var pidFileURL: URL {
        bridgeDirectoryURL.appendingPathComponent("bridge.pid")
    }

    private static func readPidFile() -> Int32? {
        guard let text = try? String(contentsOf: pidFileURL, encoding: .utf8),
              let pid = Int32(text.trimmingCharacters(in: .whitespacesAndNewlines)),
              pid > 0 else {
            return nil
        }
        return pid
    }

    private static func removePidFile() {
        try? FileManager.default.removeItem(at: pidFileURL)
    }

    /// Terminate a previously-spawned bridge recorded in the pid file. Verifies
    /// the pid is actually our bridge (guards against pid reuse) before signaling.
    private static func reclaimOrphanBridge() {
        defer { removePidFile() }
        guard let pid = readPidFile(), kill(pid, 0) == 0, processIsOurBridge(pid) else {
            return
        }
        kill(pid, SIGTERM)
        for _ in 0..<20 {           // wait up to ~1s for it to release the port
            if kill(pid, 0) != 0 { return }
            usleep(50_000)
        }
        if kill(pid, 0) == 0 {
            kill(pid, SIGKILL)      // last resort
        }
    }

    private static func stopOwnedBridge() {
        if ownsBridgeProcess, process?.isRunning == true {
            process?.terminate()
            process?.waitUntilExit()
        }
        process = nil
        ownsBridgeProcess = false
        startAttempted = false
        closeLogHandles()
    }

    private static func installedBridgeBuildID() throws -> String {
        let data = try Data(contentsOf: scriptURL)
        return SHA256.hash(data: data).prefix(8).map { String(format: "%02x", $0) }.joined()
    }

    private static func authenticatedRequest(url: URL, timeout: TimeInterval) -> URLRequest {
        var request = URLRequest(url: url, timeoutInterval: timeout)
        if let token = bridgeAuthToken() {
            request.setValue(token, forHTTPHeaderField: "X-CodexBuddy-Token")
        }
        return request
    }

    private static func bridgeHealth() async -> BridgeHealth? {
        guard let url = URL(string: "http://127.0.0.1:8789/health") else { return nil }
        do {
            let (data, response) = try await URLSession.shared.data(
                for: authenticatedRequest(url: url, timeout: 1.0)
            )
            guard (response as? HTTPURLResponse)?.statusCode == 200 else { return nil }
            return try JSONDecoder().decode(BridgeHealth.self, from: data)
        } catch {
            return nil
        }
    }

    private static func waitForBridgeHealth(
        expectedBuildID: String,
        expectedPID: Int32
    ) async -> BridgeHealth? {
        for _ in 0..<30 {
            if let health = await bridgeHealth(),
               health.status == "ok",
               health.buildID == expectedBuildID,
               health.pid == expectedPID {
                return health
            }
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        return nil
    }

    /// True if `pid` is a live process whose command line is our bridge script,
    /// so we never signal an unrelated process that happens to reuse the pid.
    private static func processIsOurBridge(_ pid: Int32) -> Bool {
        let probe = Process()
        probe.executableURL = URL(fileURLWithPath: "/bin/ps")
        probe.arguments = ["-p", String(pid), "-o", "command="]
        let pipe = Pipe()
        probe.standardOutput = pipe
        probe.standardError = FileHandle.nullDevice
        do {
            try probe.run()
            probe.waitUntilExit()
        } catch {
            return false
        }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        let command = String(data: data, encoding: .utf8) ?? ""
        return command.contains("codex_usage_server.py")
    }

    private static func closeLogHandles() {
        try? stdoutHandle?.close()
        try? stderrHandle?.close()
        stdoutHandle = nil
        stderrHandle = nil
    }

    static func diagnostics() async -> BridgeDiagnostics {
        let fileManager = FileManager.default
        let bridgeInstalled = fileManager.fileExists(atPath: scriptURL.path)
        let codexAuthFound = fileManager.fileExists(atPath: NSString(string: "~/.codex/auth.json").expandingTildeInPath)
        let bridgeRunning = await canReachBridge()
        let claudeUsageStatus = readClaudeUsageStatus()

        return BridgeDiagnostics(
            bridgeInstalled: bridgeInstalled,
            bridgeRunning: bridgeRunning,
            codexAuthFound: codexAuthFound,
            claudeUsageStatus: claudeUsageStatus,
            appSupportPath: appSupportURL.path,
            logsPath: logsDirectoryURL.path
        )
    }

    /// Ask a healthy bridge to refresh Claude OAuth and report whether restart
    /// guidance is appropriate. Authentication failures do not imply that the
    /// bridge itself needs restarting.
    static func refreshClaudeLogin() async -> ClaudeLoginRefreshResult {
        do {
            try await startIfNeeded()
        } catch {
            return ClaudeLoginRefreshResult(
                succeeded: false,
                message: "Bridge could not start: \(error.localizedDescription)",
                restartRecommended: true
            )
        }

        guard await bridgeHealth() != nil else {
            return ClaudeLoginRefreshResult(
                succeeded: false,
                message: "Bridge is not responding.",
                restartRecommended: true
            )
        }

        guard let url = URL(string: "http://127.0.0.1:8789/claude/refresh") else {
            return ClaudeLoginRefreshResult(
                succeeded: false,
                message: "Invalid bridge URL.",
                restartRecommended: false
            )
        }
        var request = URLRequest(url: url, timeoutInterval: 30)
        request.httpMethod = "POST"
        if let token = bridgeAuthToken() {
            request.setValue(token, forHTTPHeaderField: "X-CodexBuddy-Token")
        }
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            if code == 200 {
                return ClaudeLoginRefreshResult(
                    succeeded: true,
                    message: (obj?["message"] as? String) ?? "Claude login refreshed.",
                    restartRecommended: false
                )
            }
            if code == 404 {
                return ClaudeLoginRefreshResult(
                    succeeded: false,
                    message: "Bridge update detected.",
                    restartRecommended: true
                )
            }
            // A permanently dead refresh token (OAuth invalid_grant) cannot be
            // renewed by the app or a bridge restart; the user must sign in again.
            if let reauth = obj?["reauth_required"] as? Bool, reauth {
                return ClaudeLoginRefreshResult(
                    succeeded: false,
                    message: (obj?["error"] as? String)
                        ?? "Claude session expired. Run `claude auth login` in Terminal to sign in again.",
                    restartRecommended: false
                )
            }
            return ClaudeLoginRefreshResult(
                succeeded: false,
                message: (obj?["error"] as? String) ?? "Claude login refresh failed (HTTP \(code)).",
                restartRecommended: false
            )
        } catch {
            return ClaudeLoginRefreshResult(
                succeeded: false,
                message: "Bridge is not responding: \(error.localizedDescription)",
                restartRecommended: true
            )
        }
    }

    /// The loopback token the bridge writes next to its script, used to
    /// authorize the state-changing /claude/refresh endpoint.
    private static func bridgeAuthToken() -> String? {
        let url = bridgeDirectoryURL.appendingPathComponent(".bridge_token")
        guard let token = try? String(contentsOf: url, encoding: .utf8) else { return nil }
        let trimmed = token.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    /// Human-readable Claude token expiry and proactive refresh timing.
    static func claudeTokenDetails() async -> ClaudeTokenDetails {
        let unknown = ClaudeTokenDetails(status: "Unknown", automaticRefresh: "Unknown")
        guard let url = URL(string: "http://127.0.0.1:8789/claude/status") else {
            return unknown
        }
        do {
            let request = authenticatedRequest(url: url, timeout: 5)
            let (data, response) = try await URLSession.shared.data(for: request)
            guard (response as? HTTPURLResponse)?.statusCode == 200 else { return unknown }
            guard let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
                return unknown
            }
            let status: String
            if let valid = obj["valid"] as? Bool, valid, let secs = obj["expires_in_seconds"] as? Int {
                status = "Valid (\(durationLabel(seconds: secs)) left)"
            } else {
                status = (obj["message"] as? String) ?? "Unknown"
            }

            let automaticRefresh: String
            if let seconds = obj["refresh_in_seconds"] as? Int {
                automaticRefresh = seconds <= 0
                    ? "On next usage refresh"
                    : "In \(durationLabel(seconds: seconds))"
            } else {
                automaticRefresh = "Not scheduled"
            }
            return ClaudeTokenDetails(status: status, automaticRefresh: automaticRefresh)
        } catch {
            return unknown
        }
    }

    private static func durationLabel(seconds: Int) -> String {
        let minutes = max(0, seconds / 60)
        if minutes < 60 { return "\(minutes)m" }
        let hours = minutes / 60
        if hours < 24 { return "\(hours)h \(minutes % 60)m" }
        return "\(hours / 24)d \(hours % 24)h"
    }

    private static func canReachBridge() async -> Bool {
        await bridgeHealth() != nil
    }

    private static func readClaudeUsageStatus() -> String {
        guard let data = try? Data(contentsOf: claudeUsageURL),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let updatedAt = json["updated_at"] as? String,
              let updatedDate = BridgeServiceDateParser.date(from: updatedAt) else {
            return "No usage this week"
        }

        let maxAge: TimeInterval = 36 * 60 * 60
        if Date().timeIntervalSince(updatedDate) > maxAge {
            return "No usage this week"
        }

        return "Updated"
    }
}

private enum BridgeServiceDateParser {
    private static let isoWithFractionalSeconds: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    private static let iso: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter
    }()

    static func date(from value: String) -> Date? {
        isoWithFractionalSeconds.date(from: value) ?? iso.date(from: value)
    }
}

enum BridgeServiceError: LocalizedError {
    case missingBundledFile(String)
    case healthCheckFailed

    var errorDescription: String? {
        switch self {
        case .missingBundledFile(let file):
            return "Bundled bridge file missing: \(file)"
        case .healthCheckFailed:
            return "Bridge launched but did not pass its health check."
        }
    }
}
