// 生成 App 图标：暗房底色 + 一张微斜的相纸（琥珀夕阳与海面）。用法：swift icon.swift 输出.png
import AppKit

let S: CGFloat = 1024
let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "icon_1024.png"
let cs = CGColorSpace(name: CGColorSpace.sRGB)!
let ctx = CGContext(data: nil, width: Int(S), height: Int(S), bitsPerComponent: 8, bytesPerRow: 0,
                    space: cs, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
func rgb(_ h: UInt32, _ a: CGFloat = 1) -> CGColor {
    CGColor(srgbRed: CGFloat((h >> 16) & 255) / 255, green: CGFloat((h >> 8) & 255) / 255, blue: CGFloat(h & 255) / 255, alpha: a)
}
func grad(_ colors: [CGColor], _ locs: [CGFloat]) -> CGGradient {
    CGGradient(colorsSpace: cs, colors: colors as CFArray, locations: locs)!
}

// 1. 底板：macOS 图标网格 824×824，圆角约 185
let plate = CGRect(x: 100, y: 100, width: 824, height: 824)
let platePath = CGPath(roundedRect: plate, cornerWidth: 185, cornerHeight: 185, transform: nil)
ctx.saveGState()
ctx.setShadow(offset: CGSize(width: 0, height: -14), blur: 30, color: rgb(0x000000, 0.45))
ctx.addPath(platePath); ctx.setFillColor(rgb(0x171412)); ctx.fillPath()
ctx.restoreGState()
ctx.saveGState()
ctx.addPath(platePath); ctx.clip()
ctx.drawLinearGradient(grad([rgb(0x2b2521), rgb(0x131110)], [0, 1]),
                       start: CGPoint(x: 512, y: 924), end: CGPoint(x: 512, y: 100), options: [])
// 安全灯的暖光晕
ctx.drawRadialGradient(grad([rgb(0xe9a23b, 0.30), rgb(0xe9a23b, 0)], [0, 1]),
                       startCenter: CGPoint(x: 512, y: 600), startRadius: 0,
                       endCenter: CGPoint(x: 512, y: 600), endRadius: 430, options: [])
ctx.restoreGState()

// 2. 相纸：略微倾斜，米白边框
ctx.saveGState()
ctx.translateBy(x: 512, y: 500)
ctx.rotate(by: -6 * .pi / 180)
let paper = CGRect(x: -235, y: -275, width: 470, height: 550)
ctx.setShadow(offset: CGSize(width: 0, height: -18), blur: 36, color: rgb(0x000000, 0.6))
ctx.addPath(CGPath(roundedRect: paper, cornerWidth: 14, cornerHeight: 14, transform: nil))
ctx.setFillColor(rgb(0xf1e8d8)); ctx.fillPath()
ctx.setShadow(offset: .zero, blur: 0, color: nil)

// 画面区域（底部留宽边，像拍立得）
let photo = CGRect(x: -205, y: -165, width: 410, height: 410)
ctx.saveGState()
ctx.addRect(photo); ctx.clip()
// 天空：暮色到琥珀
ctx.drawLinearGradient(grad([rgb(0x3a3350), rgb(0xb8643a), rgb(0xf0b058)], [0, 0.62, 1]),
                       start: CGPoint(x: 0, y: photo.maxY), end: CGPoint(x: 0, y: -10), options: [])
// 太阳
let sun = CGPoint(x: 0, y: 20)
ctx.drawRadialGradient(grad([rgb(0xffe2a8, 0.9), rgb(0xf6b04d, 0)], [0, 1]),
                       startCenter: sun, startRadius: 0, endCenter: sun, endRadius: 150, options: [])
ctx.setFillColor(rgb(0xfff1cf))
ctx.fillEllipse(in: CGRect(x: sun.x - 62, y: sun.y - 62, width: 124, height: 124))
// 海面
let horizon: CGFloat = -10
ctx.setFillColor(rgb(0x1d1c24))
ctx.fill(CGRect(x: photo.minX, y: photo.minY, width: photo.width, height: horizon - photo.minY))
// 水面上的太阳倒影：几道琥珀色短横
for (i, w) in [150, 118, 92, 70, 50, 34].enumerated() {
    let y = horizon - 22 - CGFloat(i) * 23
    ctx.setFillColor(rgb(0xf0b058, 0.85 - CGFloat(i) * 0.11))
    ctx.fill(CGRect(x: -CGFloat(w) / 2, y: y, width: CGFloat(w), height: 7))
}
ctx.restoreGState()
// 画面内描边
ctx.setStrokeColor(rgb(0x000000, 0.12)); ctx.setLineWidth(2); ctx.stroke(photo)
ctx.restoreGState()

let img = ctx.makeImage()!
let rep = NSBitmapImageRep(cgImage: img)
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: out))
print("wrote", out)
