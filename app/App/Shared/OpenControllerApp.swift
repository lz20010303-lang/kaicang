import SwiftUI

@main
struct OpenControllerApp: App {
    @StateObject private var model = AppModel()
    @Environment(\.scenePhase) private var phase

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(model)
                .onAppear { model.start(); Task { await model.refreshAll() } }
                .onChange(of: phase, perform: { p in
                    if p == .active { model.start() } else if p == .background { model.stop() }
                })
                #if os(macOS)
                .frame(minWidth: 460, minHeight: 720)
                #endif
        }
    }
}
