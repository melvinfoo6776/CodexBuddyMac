import SwiftUI
import AppKit

struct StatusPopoverView: View {
    @EnvironmentObject private var poller: UsagePoller

    var body: some View {
        VStack(spacing: 0) {
            header

            VStack(spacing: 0) {
                ServiceDetailView(title: AppSettings.codexLabel, usage: poller.usage.codex, tint: .cyan)
                Divider()
                    .padding(.leading, 14)
                ServiceDetailView(title: AppSettings.claudeLabel, usage: poller.usage.claude, tint: .indigo)
            }

            Divider()

            footer
        }
        .frame(width: 320)
        .background(.ultraThinMaterial)
    }

    private var header: some View {
        VStack(spacing: 8) {
            HStack {
                Label("AI Usage", systemImage: poller.isOnline ? "bolt.horizontal.circle.fill" : "exclamationmark.circle.fill")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(poller.isOnline ? Color.primary : Color.orange)

                Spacer()

                if let label = poller.usage.displayLabel ?? poller.usage.statusLabel {
                    Text(label)
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }

            HStack(spacing: 8) {
                StatusPill(text: poller.isOnline ? "Online" : "Offline", tint: poller.isOnline ? .green : .orange)

                if let updatedAt = poller.usage.updatedAt {
                    Text(updatedAt)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
            }
            .font(.caption2)
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
    }

    private var footer: some View {
        VStack(spacing: 0) {
            if let error = poller.lastError {
                HStack(spacing: 8) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange)
                    Text(error)
                        .lineLimit(2)
                }
                .font(.caption2)
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 14)
                .padding(.vertical, 8)

                Divider()
            }

            Text("Bridge: \(poller.bridgeURLText)")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .truncationMode(.middle)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 14)
                .padding(.vertical, 8)

            Divider()

            VStack(spacing: 0) {
                Button {
                    Task { await poller.refresh() }
                } label: {
                    MenuActionRow(title: "Refresh Now", systemImage: "arrow.clockwise")
                }
                .buttonStyle(.plain)

                Button {
                    NSApp.sendAction(Selector(("showSettingsWindow:")), to: nil, from: nil)
                    NSApp.activate(ignoringOtherApps: true)
                } label: {
                    MenuActionRow(title: "Settings", systemImage: "gearshape")
                }
                .buttonStyle(.plain)

                Button {
                    NSApplication.shared.terminate(nil)
                } label: {
                    MenuActionRow(title: "Quit CodexBuddy", systemImage: "power")
                }
                .buttonStyle(.plain)
            }
        }
    }
}

struct ServiceDetailView: View {
    let title: String
    let usage: ServiceUsage
    let tint: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(title)
                    .font(.system(size: 12, weight: .semibold))

                Spacer()

                Text("\(usage.fiveHour.remaining) left")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }

            if let warning = usage.warning {
                Label(warning, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption2)
                    .foregroundStyle(.orange)
            }

            UsageBar(label: "5H", window: usage.fiveHour, tint: tint)
            UsageBar(label: "Week", window: usage.weekly, tint: tint.opacity(0.75))
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }
}

struct UsageBar: View {
    let label: String
    let window: UsageWindow
    let tint: Color

    var body: some View {
        HStack(spacing: 10) {
            Text(label)
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(.secondary)
                .frame(width: 34, alignment: .leading)

            GeometryReader { proxy in
                ZStack(alignment: .leading) {
                    RoundedRectangle(cornerRadius: 2)
                        .fill(Color.secondary.opacity(0.18))

                    RoundedRectangle(cornerRadius: 2)
                        .fill(barColor)
                        .frame(width: proxy.size.width * CGFloat(window.usedPercent) / 100)
                }
            }
            .frame(height: 7)

            VStack(alignment: .trailing, spacing: 1) {
                Text("\(window.usedPercent)%")
                    .font(.system(size: 11, weight: .semibold))
                    .monospacedDigit()
                    .foregroundStyle(.primary)

                Text("\(window.used)/\(window.limit)")
                    .font(.system(size: 9))
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
            }
            .frame(width: 54, alignment: .trailing)
        }
        .frame(height: 18)
    }

    private var barColor: Color {
        switch window.usedPercent {
        case 85...:
            return .red
        case 65..<85:
            return .orange
        default:
            return tint
        }
    }
}

struct StatusPill: View {
    let text: String
    let tint: Color

    var body: some View {
        HStack(spacing: 4) {
            Circle()
                .fill(tint)
                .frame(width: 6, height: 6)

            Text(text)
        }
        .padding(.horizontal, 7)
        .padding(.vertical, 3)
        .background(tint.opacity(0.12), in: Capsule())
        .foregroundStyle(tint)
    }
}

struct MenuActionRow: View {
    let title: String
    let systemImage: String

    var body: some View {
        HStack(spacing: 9) {
            Image(systemName: systemImage)
                .font(.system(size: 12))
                .foregroundStyle(.secondary)
                .frame(width: 16)

            Text(title)
                .font(.system(size: 13))
                .foregroundStyle(.primary)

            Spacer()
        }
        .contentShape(Rectangle())
        .padding(.horizontal, 14)
        .padding(.vertical, 7)
    }
}

extension UsagePoller {
    var menuBarStatusText: String {
        "\(AppSettings.codexLabel.prefix(2)) \(usage.codex.fiveHour.usedPercent)%  \(AppSettings.claudeLabel.prefix(2)) \(usage.claude.fiveHour.usedPercent)%"
    }

    var menuBarBatterySymbol: String {
        guard isOnline else { return "exclamationmark.circle" }

        let percent = max(usage.codex.fiveHour.usedPercent, usage.claude.fiveHour.usedPercent)
        switch percent {
        case 0..<25:
            return "battery.25percent"
        case 25..<50:
            return "battery.50percent"
        case 50..<75:
            return "battery.75percent"
        default:
            return "battery.100percent"
        }
    }

}
