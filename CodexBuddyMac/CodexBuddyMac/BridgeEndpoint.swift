import Foundation

struct BridgeEndpoint {
    private static let rememberedURLKey = "rememberedBridgeURL"

    static var fallbackURLs: [URL] {
        [
            URL(string: "http://127.0.0.1:8789/usage.json")!,
            URL(string: "http://127.0.0.1:8787/usage.json")!
        ]
    }

    static func candidates() -> [URL] {
        var urls: [URL] = []

        if let configured = URL(string: AppSettings.bridgeURLText) {
            urls.append(configured)
        }

        if let remembered = UserDefaults.standard.string(forKey: rememberedURLKey),
           let url = URL(string: remembered) {
            urls.append(url)
        }

        if AppSettings.includeLocalFallbacks {
            urls.append(contentsOf: fallbackURLs)
        }

        var seen = Set<String>()
        return urls.filter { seen.insert($0.absoluteString).inserted }
    }

    static func remember(_ url: URL) {
        UserDefaults.standard.set(url.absoluteString, forKey: rememberedURLKey)
    }
}
