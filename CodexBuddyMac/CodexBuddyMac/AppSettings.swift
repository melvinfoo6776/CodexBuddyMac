import Foundation

enum AppSettings {
    static let bridgeURLKey = "bridgeURL"
    static let includeLocalFallbacksKey = "includeLocalBridgeFallbacks"
    static let refreshIntervalKey = "refreshIntervalSeconds"
    static let codexLabelKey = "codexServiceLabel"
    static let claudeLabelKey = "claudeServiceLabel"

    static let defaultBridgeURLText = "http://127.0.0.1:8789/usage.json"
    static let defaultRefreshInterval = 60.0
    static let minimumRefreshInterval = 10.0

    private static var defaults: UserDefaults {
        UserDefaults.standard
    }

    static var bridgeURLText: String {
        let value = defaults.string(forKey: bridgeURLKey) ?? defaultBridgeURLText
        return value
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .replacingOccurrences(of: "http://localhost:", with: "http://127.0.0.1:")
    }

    static var includeLocalFallbacks: Bool {
        if defaults.object(forKey: includeLocalFallbacksKey) == nil {
            return true
        }
        return defaults.bool(forKey: includeLocalFallbacksKey)
    }

    static var refreshInterval: TimeInterval {
        let value = defaults.double(forKey: refreshIntervalKey)
        guard value > 0 else { return defaultRefreshInterval }
        return max(minimumRefreshInterval, value)
    }

    static var codexLabel: String {
        serviceLabel(for: codexLabelKey, fallback: "CODEX")
    }

    static var claudeLabel: String {
        serviceLabel(for: claudeLabelKey, fallback: "CLAUDE")
    }

    static func configureDefaults() {
        defaults.register(defaults: [
            bridgeURLKey: defaultBridgeURLText,
            includeLocalFallbacksKey: true,
            refreshIntervalKey: defaultRefreshInterval,
            codexLabelKey: "CODEX",
            claudeLabelKey: "CLAUDE"
        ])
    }

    private static func serviceLabel(for key: String, fallback: String) -> String {
        let value = defaults.string(forKey: key)?.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let value, !value.isEmpty else { return fallback }
        return value
    }
}
