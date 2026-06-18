import SwiftUI
import WidgetKit

struct WidgetView: View {
    let entry: CodexBuddyEntry
    @Environment(\.widgetFamily) private var family

    var body: some View {
        switch family {
        case .systemSmall:
            SmallWidgetView(usage: entry.usage)
        default:
            MediumWidgetView(usage: entry.usage)
        }
    }
}
struct MediumWidgetView: View {
    let usage: CodexBuddyUsage

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("AI USAGE")
                    .font(.caption)
                    .fontWeight(.bold)
                Spacer()
                Text(usage.displayLabel ?? usage.statusLabel ?? "WAITING")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }

            HStack(spacing: 10) {
                WidgetServiceView(title: "CODEX", usage: usage.codex)
                WidgetServiceView(title: "CLAUDE", usage: usage.claude)
            }
        }
        .padding()
    }
}

struct SmallWidgetView: View {
    let usage: CodexBuddyUsage

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("CODEX")
                .font(.caption)
                .fontWeight(.bold)

            BigPercent(value: usage.codex.fiveHour.usedPercent, label: "5H")

            Divider()

            Text("CLAUDE \(usage.claude.fiveHour.usedPercent)%")
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        .padding()
    }
}

struct WidgetServiceView: View {
    let title: String
    let usage: ServiceUsage

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title)
                .font(.caption)
                .fontWeight(.bold)

            WidgetUsageRow(label: "5H", window: usage.fiveHour)
            WidgetUsageRow(label: "WK", window: usage.weekly)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct WidgetUsageRow: View {
    let label: String
    let window: UsageWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(label)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                Spacer()
                Text("\(window.usedPercent)%")
                    .font(.caption2)
                    .monospacedDigit()
            }

            GeometryReader { proxy in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.secondary.opacity(0.18))
                    Capsule()
                        .fill(barColor)
                        .frame(width: proxy.size.width * CGFloat(window.usedPercent) / 100)
                }
            }
            .frame(height: 6)
        }
    }

    private var barColor: Color {
        switch window.usedPercent {
        case 80...:
            return .red
        case 60..<80:
            return .orange
        default:
            return .green
        }
    }
}

struct BigPercent: View {
    let value: Int
    let label: String

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("\(value)%")
                .font(.system(size: 34, weight: .bold, design: .rounded))
                .monospacedDigit()

            Text(label)
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
    }
}
