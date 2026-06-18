import Foundation

struct CodexBuddyUsage: Codable, Equatable {
    var plan: String?
    var updatedAt: String?
    var statusLabel: String?
    var displayLabel: String?
    var codex: ServiceUsage
    var claude: ServiceUsage

    enum CodingKeys: String, CodingKey {
        case plan
        case updatedAt = "updated_at"
        case statusLabel = "status_label"
        case displayLabel = "display_label"
        case codex
        case claude
    }
}

struct ServiceUsage: Codable, Equatable {
    var fiveHour: UsageWindow
    var weekly: UsageWindow
    var warning: String?
    var updatedAt: String?
    var source: String?

    enum CodingKeys: String, CodingKey {
        case fiveHour = "five_hour"
        case weekly
        case warning
        case updatedAt = "updated_at"
        case source
    }
}

struct UsageWindow: Codable, Equatable {
    var used: Int
    var limit: Int
    var remaining: Int
    var resetAt: String?

    enum CodingKeys: String, CodingKey {
        case used
        case limit
        case remaining
        case resetAt = "reset_at"
    }

    var usedPercent: Int {
        guard limit > 0 else { return 0 }
        return max(0, min(100, Int((Double(used) / Double(limit)) * 100.0)))
    }

    var remainingPercent: Int {
        guard limit > 0 else { return 0 }
        return max(0, min(100, Int((Double(remaining) / Double(limit)) * 100.0)))
    }
}

extension CodexBuddyUsage {
    static let empty = CodexBuddyUsage(
        plan: nil,
        updatedAt: nil,
        statusLabel: "WAITING",
        displayLabel: "WAITING",
        codex: ServiceUsage.empty,
        claude: ServiceUsage.empty
    )
}

extension ServiceUsage {
    static let empty = ServiceUsage(
        fiveHour: UsageWindow(used: 0, limit: 100, remaining: 0, resetAt: nil),
        weekly: UsageWindow(used: 0, limit: 100, remaining: 0, resetAt: nil),
        warning: nil,
        updatedAt: nil,
        source: nil
    )

    static let claudeNotRunThisWeek = ServiceUsage(
        fiveHour: UsageWindow(used: 0, limit: 100, remaining: 100, resetAt: nil),
        weekly: UsageWindow(used: 0, limit: 100, remaining: 100, resetAt: nil),
        warning: "Claude has not been run this week.",
        updatedAt: nil,
        source: nil
    )
}
