import AppKit
import Foundation
import Vision

struct OCRResult: Codable {
    let sourceIndex: Int
    let path: String
    let text: String
    let confidence: Float?
    let error: String?
}

var output: [OCRResult] = []
let arguments = Array(CommandLine.arguments.dropFirst())
for offset in stride(from: 0, to: arguments.count, by: 2) {
    guard offset + 1 < arguments.count, let sourceIndex = Int(arguments[offset]) else { continue }
    let path = arguments[offset + 1]
    guard let image = NSImage(contentsOfFile: path) else {
        output.append(OCRResult(sourceIndex: sourceIndex, path: path, text: "", confidence: nil, error: "image_decode_failed"))
        continue
    }
    var rect = NSRect(origin: .zero, size: image.size)
    guard let cgImage = image.cgImage(forProposedRect: &rect, context: nil, hints: nil) else {
        output.append(OCRResult(sourceIndex: sourceIndex, path: path, text: "", confidence: nil, error: "cgimage_conversion_failed"))
        continue
    }
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
            output.append(OCRResult(sourceIndex: sourceIndex, path: path, text: text, confidence: confidence, error: nil))
        }
    } catch {
        output.append(OCRResult(sourceIndex: sourceIndex, path: path, text: "", confidence: nil, error: "vision_request_failed"))
    }
}

let encoder = JSONEncoder()
encoder.outputFormatting = [.withoutEscapingSlashes]
if let data = try? encoder.encode(output), let json = String(data: data, encoding: .utf8) {
    print(json)
} else {
    print("[]")
}
