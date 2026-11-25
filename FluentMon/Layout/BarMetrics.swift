import CoreGraphics

struct BarMetrics: Equatable {
    var scale: CGFloat = 1
    var showCaptions = true
    var padV: CGFloat { 14 * scale }
    var row: CGFloat { (22 + 8 + 12 + (showCaptions ? 6 + 18 : 0)) * scale }
    var rowGap: CGFloat { 14 * scale }
    var height: CGFloat { 2 * padV + 3 * row + 2 * rowGap }
    var compact: BarMetrics { var c = self; c.showCaptions = false; return c }
}
