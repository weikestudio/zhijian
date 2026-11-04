import Foundation
import Vision
import AppKit

// macOS 原生 OCR：venv 里装 pyobjc 会被沙箱杀掉，用系统 Vision 最稳
// 用法: ocr <image>   输出逐行文本

let args = CommandLine.arguments
guard args.count > 1 else {
    FileHandle.standardError.write("用法: ocr <image>\n".data(using: .utf8)!)
    exit(1)
}
let path = args[1]
guard let img = NSImage(contentsOfFile: path),
      let tiff = img.tiffRepresentation,
      let bmp = NSBitmapImageRep(data: tiff),
      let cg = bmp.cgImage else {
    FileHandle.standardError.write("无法读取图片: \(path)\n".data(using: .utf8)!)
    exit(2)
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.recognitionLanguages = ["zh-Hans", "en-US"]
request.usesLanguageCorrection = true

let handler = VNImageRequestHandler(cgImage: cg, options: [:])
do {
    try handler.perform([request])
} catch {
    FileHandle.standardError.write("OCR 失败: \(error)\n".data(using: .utf8)!)
    exit(3)
}

if let results = request.results {
    for obs in results {
        if let top = obs.topCandidates(1).first {
            print(top.string)
        }
    }
}
