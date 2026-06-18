import AppKit
import Combine
import SwiftUI

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let poller = UsagePoller()

    private var statusItem: NSStatusItem?
    private var popover: NSPopover?
    private var cancellables = Set<AnyCancellable>()

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        setupStatusItem()
        setupPopover()
        bindStatusUpdates()
    }

    private func setupStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem = item

        guard let button = item.button else {
            return
        }

        button.target = self
        button.action = #selector(togglePopover)
        button.imagePosition = .imageLeft
        updateStatusButton()
    }

    private func setupPopover() {
        let popover = NSPopover()
        popover.behavior = .transient
        popover.contentSize = NSSize(width: 320, height: 305)
        popover.contentViewController = NSHostingController(
            rootView: StatusPopoverView()
                .environmentObject(poller)
        )
        self.popover = popover
    }

    private func bindStatusUpdates() {
        poller.$usage
            .combineLatest(poller.$isOnline)
            .sink { [weak self] _, _ in
                self?.updateStatusButton()
            }
            .store(in: &cancellables)
    }

    private func updateStatusButton() {
        guard let button = statusItem?.button else {
            return
        }

        button.image = NSImage(systemSymbolName: poller.menuBarBatterySymbol, accessibilityDescription: nil)
        button.title = " \(poller.menuBarStatusText)"
        button.toolTip = "CodexBuddy 5-hour usage"
    }

    @objc private func togglePopover() {
        guard let button = statusItem?.button, let popover else {
            return
        }

        if popover.isShown {
            popover.performClose(nil)
        } else {
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
            popover.contentViewController?.view.window?.makeKey()
        }
    }
}
