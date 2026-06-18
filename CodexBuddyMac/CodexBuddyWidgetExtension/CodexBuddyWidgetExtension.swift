import WidgetKit
import SwiftUI

struct CodexBuddyEntry: TimelineEntry {
    let date: Date
    let usage: CodexBuddyUsage
}
struct CodexBuddyProvider: TimelineProvider {
    private let store = UsageStore()

    func placeholder(in context: Context) -> CodexBuddyEntry {
        CodexBuddyEntry(date: Date(), usage: .empty)
    }

    func getSnapshot(in context: Context, completion: @escaping (CodexBuddyEntry) -> Void) {
        completion(CodexBuddyEntry(date: Date(), usage: store.load()))
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<CodexBuddyEntry>) -> Void) {
        let entry = CodexBuddyEntry(date: Date(), usage: store.load())
        let next = Calendar.current.date(byAdding: .minute, value: 15, to: Date()) ?? Date().addingTimeInterval(900)
        completion(Timeline(entries: [entry], policy: .after(next)))
    }
}

struct CodexBuddyWidget: Widget {
    let kind = "CodexBuddyWidget"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: CodexBuddyProvider()) { entry in
            WidgetView(entry: entry)
                .containerBackground(.fill.tertiary, for: .widget)
        }
        .configurationDisplayName("CodexBuddy")
        .description("Shows Codex and Claude usage from your local bridge.")
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge])
    }
}
