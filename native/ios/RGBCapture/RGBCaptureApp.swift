import SwiftUI

@main
struct RGBCaptureApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
        }
    }
}

struct ContentView: View {
    @StateObject private var model = CaptureModel()

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            TextField("https://192.168.1.160:8765", text: $model.base)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .textFieldStyle(.roundedBorder)
            Text(model.status).font(.footnote)
            Button(model.recording ? "Stop" : "Record") { model.toggleRecord() }
                .buttonStyle(.borderedProminent)
            CameraPreview(session: model.session)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .padding()
        .onAppear { model.start() }
    }
}
