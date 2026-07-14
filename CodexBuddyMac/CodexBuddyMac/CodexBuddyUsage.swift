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

    var resetDate: Date? {
        guard let resetAt, !resetAt.isEmpty else { return nil }
        return UsageDateParser.date(from: resetAt)
    }

    var isAvailable: Bool {
        used >= 0 && remaining >= 0 && limit > 0
    }
}

extension ServiceUsage {
    var primaryWindow: UsageWindow {
        fiveHour.isAvailable ? fiveHour : weekly
    }

    var primaryWindowLabel: String {
        fiveHour.isAvailable ? "5H" : "Week"
    }

    var hasAvailableWindow: Bool {
        fiveHour.isAvailable || weekly.isAvailable
    }

    var nextResetText: String? {
        let now = Date()
        let candidates: [(String, Date)] = [
            ("5H", fiveHour.isAvailable ? fiveHour.resetDate : nil),
            ("Week", weekly.isAvailable ? weekly.resetDate : nil)
        ].compactMap { label, date in
            guard let date, date > now else { return nil }
            return (label, date)
        }
        guard let next = candidates.min(by: { $0.1 < $1.1 }) else { return nil }
        let actual = UsageDateParser.actualLabel(from: now, to: next.1)
        let countdown = UsageDateParser.relativeLabel(from: now, to: next.1)
        return "Next reset: \(next.0) \(actual) (\(countdown))"
    }
}

private enum UsageDateParser {
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

    static func actualLabel(from now: Date, to date: Date) -> String {
        let formatter = DateFormatter()
        formatter.locale = .autoupdatingCurrent
        formatter.timeZone = .autoupdatingCurrent
        formatter.timeStyle = .short
        if Calendar.autoupdatingCurrent.isDate(date, inSameDayAs: now) {
            return "at \(formatter.string(from: date))"
        }
        formatter.dateStyle = .medium
        return "on \(formatter.string(from: date))"
    }

    static func relativeLabel(from now: Date, to date: Date) -> String {
        let totalMinutes = max(0, Int(date.timeIntervalSince(now) / 60))
        if totalMinutes < 60 { return "in \(totalMinutes)m" }
        let hours = totalMinutes / 60
        if hours < 24 { return "in \(hours)h \(totalMinutes % 60)m" }
        return "in \(hours / 24)d \(hours % 24)h"
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
