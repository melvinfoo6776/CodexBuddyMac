import Foundation
import ServiceManagement

enum LoginItemService {
    static var isEnabled: Bool {
        SMAppService.mainApp.status == .enabled
    }

    static var statusText: String {
        switch SMAppService.mainApp.status {
        case .enabled:
            return "Enabled"
        case .notRegistered:
            return "Off"
        case .notFound:
            return "App not installed"
        case .requiresApproval:
            return "Needs approval in System Settings"
        @unknown default:
            return "Unknown"
        }
    }

    static func setEnabled(_ enabled: Bool) throws {
        if enabled {
            try SMAppService.mainApp.register()
        } else {
            try SMAppService.mainApp.unregister()
        }
    }
}
