import AVFoundation
import Foundation
import Network
import SwiftUI
import VideoToolbox

final class CaptureModel: NSObject, ObservableObject, URLSessionDelegate, AVCaptureVideoDataOutputSampleBufferDelegate {
    @Published var base = "https://192.168.1.160:8765"
    @Published var status = "Set the RGB MoCap address."
    @Published var recording = false
    let session = AVCaptureSession()
    private let queue = DispatchQueue(label: "rgb.capture")
    private var compression: VTCompressionSession?
    private var connection: NWConnection?
    private var headerSent = false
    private var accepting = false
    private var epoch: Double?
    private var offset = 0.0
    private var previewBusy = false
    private var frameIndex = 0
    private lazy var http: URLSession = {
        URLSession(configuration: .ephemeral, delegate: self, delegateQueue: nil)
    }()

    func start() {
        queue.async { self.configureCamera() }
        post("/hello", body: "{\"role\":\"iphone\"}") { _ in }
        poll()
    }

    func toggleRecord() {
        let path = recording ? "/stop" : "/start"
        post(path, body: "{}") { _ in }
    }

    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        if challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
           let trust = challenge.protectionSpace.serverTrust {
            completionHandler(.useCredential, URLCredential(trust: trust))
            return
        }
        completionHandler(.performDefaultHandling, nil)
    }

    private func poll() {
        let started = Date()
        get("/status") { [weak self] text in
            guard let self else { return }
            let rtt = Date().timeIntervalSince(started)
            if let text {
                let recording = text.contains("\"recording\": true") || text.contains("\"recording\":true")
                let accepting = text.contains("\"accepting_video\": true") || text.contains("\"accepting_video\":true")
                let epoch = self.number(text, key: "epoch")
                if let server = self.number(text, key: "server_time") {
                    self.offset = server - Date().timeIntervalSince1970 - rtt / 2
                }
                DispatchQueue.main.async {
                    self.recording = recording
                    self.status = recording ? "Recording" : "Connected"
                }
                self.queue.async {
                    self.epoch = epoch
                    self.accepting = accepting
                    if recording && accepting {
                        self.ensureStream(from: text)
                    } else {
                        self.closeStream()
                    }
                }
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { self.poll() }
        }
    }

    private func configureCamera() {
        session.beginConfiguration()
        session.sessionPreset = .inputPriority
        guard let device = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back),
              let input = try? AVCaptureDeviceInput(device: device),
              session.canAddInput(input) else {
            DispatchQueue.main.async { self.status = "No camera." }
            return
        }
        session.addInput(input)
        let output = AVCaptureVideoDataOutput()
        output.alwaysDiscardsLateVideoFrames = true
        output.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_420YpCbCr8BiPlanarFullRange]
        output.setSampleBufferDelegate(self, queue: queue)
        guard session.canAddOutput(output) else { return }
        session.addOutput(output)
        if let format = bestFormat(device) {
            try? device.lockForConfiguration()
            device.activeFormat = format.format
            device.activeVideoMinFrameDuration = format.range.minFrameDuration
            device.activeVideoMaxFrameDuration = format.range.minFrameDuration
            device.unlockForConfiguration()
        }
        session.commitConfiguration()
        session.startRunning()
        let dimensions = CMVideoFormatDescriptionGetDimensions(device.activeFormat.formatDescription)
        DispatchQueue.main.async { self.status = "Camera \(dimensions.width)x\(dimensions.height)" }
        makeEncoder(width: Int(dimensions.width), height: Int(dimensions.height))
    }

    private func bestFormat(_ device: AVCaptureDevice) -> (format: AVCaptureDevice.Format, range: AVFrameRateRange)? {
        var choice: (AVCaptureDevice.Format, AVFrameRateRange, Int32)?
        for format in device.formats {
            let dimensions = CMVideoFormatDescriptionGetDimensions(format.formatDescription)
            for range in format.videoSupportedFrameRateRanges where range.maxFrameRate >= 24 {
                let fps = min(range.maxFrameRate, 60)
                let score = dimensions.width * 1000 + Int32(fps)
                if choice == nil || score > choice!.2 {
                    choice = (format, range, score)
                }
            }
        }
        return choice.map { ($0.0, $0.1) }
    }

    private func makeEncoder(width: Int, height: Int) {
        var session: VTCompressionSession?
        VTCompressionSessionCreate(allocator: nil, width: Int32(width), height: Int32(height),
                                   codecType: kCMVideoCodecType_HEVC, encoderSpecification: nil,
                                   imageBufferAttributes: nil, compressedDataAllocator: nil,
                                   outputCallback: encodeCallback, refcon: Unmanaged.passUnretained(self).toOpaque(),
                                   compressionSessionOut: &session)
        guard let session else { return }
        VTSessionSetProperty(session, key: kVTCompressionPropertyKey_RealTime, value: kCFBooleanTrue)
        VTSessionSetProperty(session, key: kVTCompressionPropertyKey_AllowFrameReordering, value: kCFBooleanFalse)
        VTSessionSetProperty(session, key: kVTCompressionPropertyKey_ProfileLevel, value: kVTProfileLevel_HEVC_Main_AutoLevel)
        VTCompressionSessionPrepareToEncodeFrames(session)
        compression = session
    }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        frameIndex += 1
        if frameIndex % 3 == 0 { sendPreview(sampleBuffer) }
        guard let session = compression, let image = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        let time = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
        VTCompressionSessionEncodeFrame(session, imageBuffer: image, presentationTimeStamp: time, duration: .invalid,
                                        frameProperties: nil, sourceFrameRefcon: nil, infoFlagsOut: nil)
    }

    private func sendPreview(_ sample: CMSampleBuffer) {
        guard !previewBusy, let buffer = CMSampleBufferGetImageBuffer(sample) else { return }
        previewBusy = true
        let image = CIImage(cvPixelBuffer: buffer)
        let scale = min(1, 960 / image.extent.width)
        let jpeg = CIContext().jpegRepresentation(of: image.transformed(by: CGAffineTransform(scaleX: scale, y: scale)), colorSpace: CGColorSpaceCreateDeviceRGB())
        previewBusy = false
        guard let jpeg else { return }
        guard let url = URL(string: base + "/preview") else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("image/jpeg", forHTTPHeaderField: "Content-Type")
        request.httpBody = jpeg
        http.dataTask(with: request).resume()
    }

    fileprivate func encoded(_ sample: CMSampleBuffer) {
        guard accepting, recordingEpoch() != nil, let annex = annexB(sample) else { return }
        let pts = UInt64(max(0, (Date().timeIntervalSince1970 + offset - (epoch ?? 0)) * 1_000_000))
        let key = !((CMGetAttachment(sample, key: kCMSampleAttachmentKey_NotSync, attachmentModeOut: nil) as? Bool) ?? false)
        send(annex, pts: pts, key: key, codec: 1)
    }

    private func recordingEpoch() -> Double? {
        epoch
    }

    private func annexB(_ sample: CMSampleBuffer) -> Data? {
        guard let block = CMSampleBufferGetDataBuffer(sample) else { return nil }
        var length = 0
        var pointer: UnsafeMutablePointer<Int8>?
        guard CMBlockBufferGetDataPointer(block, atOffset: 0, lengthAtOffsetOut: nil, totalLengthOut: &length, dataPointerOut: &pointer) == noErr,
              let pointer else { return nil }
        var nalLength: Int32 = 4
        var out = Data()
        if let format = CMSampleBufferGetFormatDescription(sample),
           (CMGetAttachment(sample, key: kCMSampleAttachmentKey_NotSync, attachmentModeOut: nil) as? Bool) != true {
            var count = 0
            CMVideoFormatDescriptionGetHEVCParameterSetAtIndex(format, parameterSetIndex: 0, parameterSetPointerOut: nil,
                                                                parameterSetSizeOut: nil, parameterSetCountOut: &count,
                                                                nalUnitHeaderLengthOut: &nalLength)
            for index in 0..<count {
                var set: UnsafePointer<UInt8>?
                var size = 0
                if CMVideoFormatDescriptionGetHEVCParameterSetAtIndex(format, parameterSetIndex: index, parameterSetPointerOut: &set,
                                                                       parameterSetSizeOut: &size, parameterSetCountOut: nil,
                                                                       nalUnitHeaderLengthOut: nil) == noErr, let set {
                    out.append(contentsOf: [0, 0, 0, 1])
                    out.append(set, count: size)
                }
            }
        }
        let headerLength = Int(nalLength)
        var offset = 0
        while offset + headerLength <= length {
            var size = 0
            for byte in 0..<headerLength {
                size = (size << 8) | Int(UInt8(bitPattern: pointer[offset + byte]))
            }
            offset += headerLength
            if size <= 0 || offset + size > length { break }
            out.append(contentsOf: [0, 0, 0, 1])
            out.append(Data(bytes: pointer + offset, count: size))
            offset += size
        }
        return out.isEmpty ? nil : out
    }

    private func ensureStream(from status: String) {
        if connection != nil { return }
        let port = NWEndpoint.Port(rawValue: UInt16(number(status, key: "video_port") ?? 8766)) ?? 8766
        guard let host = URL(string: base)?.host else { return }
        let endpoint = NWEndpoint.hostPort(host: NWEndpoint.Host(host), port: port)
        let link = NWConnection(to: endpoint, using: .tcp)
        link.stateUpdateHandler = { [weak self] state in
            if case .ready = state {
                self?.queue.async {
                    self?.headerSent = false
                    self?.send(Data(), pts: 0, key: false, codec: 1, headerOnly: true)
                }
            }
        }
        link.start(queue: queue)
        connection = link
    }

    private func closeStream() {
        connection?.cancel()
        connection = nil
        headerSent = false
    }

    private func send(_ unit: Data, pts: UInt64, key: Bool, codec: UInt8, headerOnly: Bool = false) {
        guard let connection else { return }
        var packet = Data()
        if !headerSent {
            packet.append(contentsOf: [0x52, 0x47, 0x42, 0x56, 0x01])
            headerSent = true
        }
        if headerOnly {
            connection.send(content: packet, completion: .contentProcessed { _ in })
            return
        }
        var size = UInt32(unit.count).bigEndian
        var time = pts.bigEndian
        packet.append(Data(bytes: &size, count: 4))
        packet.append(Data(bytes: &time, count: 8))
        packet.append(key ? 1 : 0)
        packet.append(codec)
        packet.append(unit)
        connection.send(content: packet, completion: .contentProcessed { _ in })
    }

    private func get(_ path: String, done: @escaping (String?) -> Void) {
        guard let url = URL(string: base + path) else { done(nil); return }
        http.dataTask(with: url) { data, response, _ in
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            done(code == 200 ? data.flatMap { String(data: $0, encoding: .utf8) } : nil)
        }.resume()
    }

    private func post(_ path: String, body: String, done: @escaping (Bool) -> Void) {
        guard let url = URL(string: base + path) else { done(false); return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = body.data(using: .utf8)
        http.dataTask(with: request) { _, response, _ in
            done((response as? HTTPURLResponse)?.statusCode == 200)
        }.resume()
    }

    private func number(_ json: String, key: String) -> Double? {
        guard let range = json.range(of: "\"\(key)\":") else { return nil }
        let rest = json[range.upperBound...].drop { $0.isWhitespace }
        if rest.hasPrefix("null") { return nil }
        let raw = rest.prefix { $0 != "," && $0 != "}" && !$0.isWhitespace }
        return Double(raw)
    }
}

private let encodeCallback: VTCompressionOutputCallback = { refcon, _, status, _, sample in
    guard status == noErr, let sample, let refcon else { return }
    Unmanaged<CaptureModel>.fromOpaque(refcon).takeUnretainedValue().encoded(sample)
}

struct CameraPreview: UIViewRepresentable {
    let session: AVCaptureSession
    func makeUIView(context: Context) -> PreviewView { PreviewView(session: session) }
    func updateUIView(_ view: PreviewView, context: Context) {}
}

final class PreviewView: UIView {
    override class var layerClass: AnyClass { AVCaptureVideoPreviewLayer.self }
    init(session: AVCaptureSession) {
        super.init(frame: .zero)
        (layer as? AVCaptureVideoPreviewLayer)?.session = session
        (layer as? AVCaptureVideoPreviewLayer)?.videoGravity = .resizeAspect
    }
    required init?(coder: NSCoder) { fatalError() }
}
