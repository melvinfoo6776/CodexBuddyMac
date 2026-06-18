import Foundation

struct BridgeDiagnostics {
    let bridgeInstalled: Bool
    let bridgeRunning: Bool
    let codexAuthFound: Bool
    let claudeUsageStatus: String
    let appSupportPath: String
    let logsPath: String
}

enum BridgeService {
    static let bridgeURL = URL(string: "http://127.0.0.1:8789/usage.json")!

    private static let bundledBridgeFiles = [
        "codex_usage_server.py",
        "usage.json",
        "claude_usage.json"
    ]

    private static var process: Process?
    private static var startAttempted = false

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
            if fileName == "codex_usage_server.py" || !fileManager.fileExists(atPath: destinationURL.path) {
                if fileManager.fileExists(atPath: destinationURL.path) {
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

    static func startIfNeeded() throws {
        try installBundledBridge()

        if process?.isRunning == true {
            return
        }

        if process?.isRunning == false {
            process = nil
            startAttempted = false
        } else if startAttempted, process == nil {
            startAttempted = false
        }

        if startAttempted {
            return
        }

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
        bridge.standardOutput = try FileHandle(forWritingTo: stdoutURL)
        bridge.standardError = try FileHandle(forWritingTo: stderrURL)

        do {
            try bridge.run()
            process = bridge
        } catch {
            startAttempted = false
            throw error
        }
    }

    static func restart() throws {
        if process?.isRunning == true {
            process?.terminate()
        }
        process = nil
        startAttempted = false
        try startIfNeeded()
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

    private static func canReachBridge() async -> Bool {
        do {
            let request = URLRequest(url: bridgeURL, timeoutInterval: 1.5)
            let (_, response) = try await URLSession.shared.data(for: request)
            return (response as? HTTPURLResponse)?.statusCode == 200
        } catch {
            return false
        }
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

    var errorDescription: String? {
        switch self {
        case .missingBundledFile(let file):
            return "Bundled bridge file missing: \(file)"
        }
    }
}
