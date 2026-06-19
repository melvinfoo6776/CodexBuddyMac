import Foundation
import Combine

@MainActor
final class UsagePoller: ObservableObject {
    @Published private(set) var usage: CodexBuddyUsage
    @Published private(set) var isOnline = false
    @Published private(set) var lastError: String?
    @Published private(set) var bridgeURLText = "Detecting bridge"

    private let store = UsageStore()
    private var timer: Timer?
    private var isRefreshing = false

    init() {
        AppSettings.configureDefaults()
        usage = store.load()
        start()
    }

    func start() {
        timer?.invalidate()
        Task { await refresh() }
        timer = Timer.scheduledTimer(withTimeInterval: AppSettings.refreshInterval, repeats: true) { [weak self] _ in
            Task { await self?.refresh() }
        }
    }

    func applySettings() {
        start()
    }

    func refresh() async {
        guard !isRefreshing else {
            return
        }

        isRefreshing = true
        defer { isRefreshing = false }

        var startupFailure: Error?
        do {
            try await BridgeService.startIfNeeded()
        } catch {
            startupFailure = error
        }

        let bridgeURLs = BridgeEndpoint.candidates()
        var lastFailure: Error?
        var lastAttemptedURL: URL?

        for bridgeURL in bridgeURLs {
            lastAttemptedURL = bridgeURL

            do {
                let (data, response) = try await URLSession.shared.data(from: bridgeURL)
                guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
                    throw URLError(.badServerResponse)
                }

                var decoded = try JSONDecoder().decode(CodexBuddyUsage.self, from: data)
                try validateFresh(decoded)
                normalizeStaleClaude(&decoded)
                usage = decoded
                isOnline = true
                lastError = nil
                bridgeURLText = bridgeURL.absoluteString
                BridgeEndpoint.remember(bridgeURL)
                store.save(decoded)
                return
            } catch {
                lastFailure = error
            }
        }

        usage = store.load()
        isOnline = false
        bridgeURLText = lastAttemptedURL?.absoluteString ?? AppSettings.bridgeURLText
        lastError = bridgeFailureMessage(from: lastFailure ?? startupFailure, attemptedURLs: bridgeURLs)
    }

    private func bridgeFailureMessage(from error: Error?, attemptedURLs: [URL]) -> String {
        let endpointList = attemptedURLs
            .map(\.absoluteString)
            .joined(separator: ", ")

        guard !endpointList.isEmpty else {
            return "No bridge endpoint configured."
        }

        if let error {
            return "\(error.localizedDescription) Tried: \(endpointList)"
        }

        return "Bridge unavailable. Tried: \(endpointList)"
    }

    private func validateFresh(_ decoded: CodexBuddyUsage) throws {
        guard let updatedAt = decoded.updatedAt,
              let updatedDate = BridgeDateParser.date(from: updatedAt) else {
            return
        }

        let maxCacheAge: TimeInterval = 36 * 60 * 60
        if Date().timeIntervalSince(updatedDate) > maxCacheAge {
            throw BridgeError.staleData(updatedAt)
        }
    }

    private func normalizeStaleClaude(_ decoded: inout CodexBuddyUsage) {
        guard let updatedAt = decoded.claude.updatedAt,
              let updatedDate = BridgeDateParser.date(from: updatedAt) else {
            return
        }

        let maxClaudeFallbackAge: TimeInterval = 36 * 60 * 60
        if Date().timeIntervalSince(updatedDate) > maxClaudeFallbackAge {
            let warning = decoded.claude.warning
            decoded.claude = .claudeNotRunThisWeek
            if let warning {
                decoded.claude.warning = warning
            }
        }
    }
}

private enum BridgeError: LocalizedError {
    case staleData(String)

    var errorDescription: String? {
        switch self {
        case .staleData(let updatedAt):
            return "Bridge data is stale: \(updatedAt)"
        }
    }
}

private enum BridgeDateParser {
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
