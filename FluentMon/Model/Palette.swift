import SwiftUI

enum Palette {
    static let cool = Color(red: 0x3B / 255, green: 0xA7 / 255, blue: 1)
    static let hot = Color(red: 1, green: 0x7A / 255, blue: 0)
    static let temperature = Color.yellow
    static let power = Color(red: 0x33 / 255, green: 0xFF / 255, blue: 0x63 / 255)
    static let fluent = Color(red: 1, green: 0x2D / 255, blue: 0x95 / 255)
    static let residuals: [Color] = [.red, .blue, .green, .orange, .purple, .cyan, .pink, .mint]
}
