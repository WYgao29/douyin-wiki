import AppKit
import Foundation
import Vision

struct OCRResult: Codable {
    let path: String
    let text: String
    let confidence: Float
}

var output: [OCRResult] = []
for path in CommandLine.arguments.dropFirst() {
    guard let image = NSImage(contentsOfFile: path) else { continue }
    var rect = NSRect(origin: .zero, size: image.size)
    guard let cgImage = image.cgImage(forProposedRect: &rect, context: nil, hints: nil) else { continue }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    request.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US"]
    let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
    do {
        try handler.perform([request])
        let observations = request.results ?? []
        let candidates = observations.compactMap { $0.topCandidates(1).first }
        let text = candidates.map(\.string).joined(separator: "\n")
        let confidence = candidates.isEmpty ? 0 : candidates.map(\.confidence).reduce(0, +) / Float(candidates.count)
        if !text.isEmpty {
            output.append(OCRResult(path: path, text: text, confidence: confidence))
        }
    } catch {
        continue
    }
}

let encoder = JSONEncoder()
encoder.outputFormatting = [.withoutEscapingSlashes]
if let data = try? encoder.encode(output), let json = String(data: data, encoding: .utf8) {
    print(json)
} else {
    print("[]")
}
