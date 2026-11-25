import SwiftUI
import UIKit

struct DashboardGeometry: Equatable {
    enum Mode { case landscape, portrait }
    static let pad: CGFloat = 16, gap: CGFloat = 12, headerHeight: CGFloat = 32
    static let vGapRatio: CGFloat = 0.20, hGapRatio: CGFloat = 0.15
    static let k: CGFloat = (1 + sin(.pi / 4)) / 2

    let mode: Mode
    let header: CGRect, chart: CGRect, bars: CGRect
    let meters: [CGRect]
    let gaugeDiameter: CGFloat

    init(size: CGSize, mode: Mode, barsHeight: CGFloat, displayScale: CGFloat) {
        let p = Self.pad, g = Self.gap, h = Self.headerHeight, k = Self.k
        let W = size.width - 2 * p, H = size.height - 2 * p
        let top = p + h + g
        let bottom = p + H
        let d: CGFloat
        let m: [CGRect], c: CGRect, b: CGRect

        switch mode {
        case .landscape:
            d = (H - h - g) / (6 * k + 5 * Self.vGapRatio)
            let s = Self.vGapRatio * d
            let xR = p + W - d
            let leftW = W - g - d
            m = (0..<6).map { i in
                CGRect(x: xR, y: top + CGFloat(i) * (k * d + s), width: d, height: k * d)
            }
            b = CGRect(x: p, y: bottom - barsHeight, width: leftW, height: barsHeight)
            c = CGRect(x: p, y: top, width: leftW, height: b.minY - g - top)
        case .portrait:
            d = W / (6 + 5 * Self.hGapRatio)
            let s = Self.hGapRatio * d
            m = (0..<6).map { i in
                CGRect(x: p + CGFloat(i) * (d + s), y: top, width: d, height: k * d)
            }
            b = CGRect(x: p, y: bottom - barsHeight, width: W, height: barsHeight)
            let chartTop = top + k * d + g
            c = CGRect(x: p, y: chartTop, width: W, height: b.minY - g - chartTop)
        }

        self.mode = mode
        header = Self.snap(CGRect(x: p, y: p, width: W, height: h), displayScale)
        chart = Self.snap(c, displayScale)
        bars = Self.snap(b, displayScale)
        meters = m.map { Self.snap($0, displayScale) }
        gaugeDiameter = d
    }

    static func snap(_ r: CGRect, _ scale: CGFloat) -> CGRect {
        let x0 = (r.minX * scale).rounded() / scale
        let y0 = (r.minY * scale).rounded() / scale
        let x1 = (r.maxX * scale).rounded() / scale
        let y1 = (r.maxY * scale).rounded() / scale
        return CGRect(x: x0, y: y0, width: x1 - x0, height: y1 - y0)
    }
}

enum LayoutMode {
    static func resolve(size: CGSize, isFullScreen: Bool, orientation: UIInterfaceOrientation,
                        previous: DashboardGeometry.Mode) -> DashboardGeometry.Mode {
        if isFullScreen, orientation != .unknown {
            return orientation.isPortrait ? .portrait : .landscape
        }
        let ratio = size.width / size.height
        switch previous {
        case .landscape: return ratio < 0.95 ? .portrait : .landscape
        case .portrait: return ratio > 1.05 ? .landscape : .portrait
        }
    }
}

extension View {
    func place(_ r: CGRect) -> some View {
        frame(width: r.width, height: r.height).offset(x: r.minX, y: r.minY)
    }
}

struct SceneState: Equatable {
    var isFullScreen = true
    var orientation: UIInterfaceOrientation = .landscapeLeft
}
