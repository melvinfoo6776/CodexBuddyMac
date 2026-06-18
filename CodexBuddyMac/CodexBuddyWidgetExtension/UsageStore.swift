import Foundation

struct UsageStore {
    static let appGroupIdentifier = "group.com.example.CodexBuddyMac"
    private static let usageKey = "latestUsageJSON"
    private static let cachedAtKey = "latestUsageCachedAt"

    var defaults: UserDefaults {
        UserDefaults(suiteName: Self.appGroupIdentifier) ?? .standard
    }

    func load() -> CodexBuddyUsage {
        guard let data = defaults.data(forKey: Self.usageKey) else {
            return .empty
        }

        do {
            return try JSONDecoder().decode(CodexBuddyUsage.self, from: data)
        } catch {
            return .empty
        }
    }

    func save(_ usage: CodexBuddyUsage) {
        guard let data = try? JSONEncoder().encode(usage) else {
            return
        }

        defaults.set(data, forKey: Self.usageKey)
        defaults.set(Date(), forKey: Self.cachedAtKey)
    }

    func cachedAt() -> Date? {
        defaults.object(forKey: Self.cachedAtKey) as? Date
    }
}
