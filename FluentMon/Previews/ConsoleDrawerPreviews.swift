import Combine
import SwiftUI

private struct ConsoleDemo: View {
    let initialLevel: ConsoleDrawer.Level
    @State private var buffer = ConsoleBuffer(lines: SampleData.consoleLines(count: 300))
    @State private var nextLine = 301

    private let timer = Timer.publish(every: 1.0 / 30, on: .main, in: .common).autoconnect()

    var body: some View {
        ZStack(alignment: .bottomLeading) {
            Color(white: 0.06)
            ConsoleDrawer(console: buffer,
                          onSubscribe: { _ in },
                          onLoadOlder: { _ in },
                          initialLevel: initialLevel)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .preferredColorScheme(.dark)
        .onReceive(timer) { _ in
            let sample = SampleData.consoleLines(count: 7)[nextLine % 7]
            buffer.append(first: nextLine, lines: [sample.text], kinds: String(sample.kind))
            nextLine += 1
        }
    }
}

private struct ConsoleCases: View {
    var body: some View {
        HStack(spacing: 24) {
            ConsoleDemo(initialLevel: .collapsed)
                .frame(width: 320)
            ConsoleDemo(initialLevel: .half)
                .frame(width: 320)
            ConsoleDemo(initialLevel: .full)
                .frame(width: 320)
        }
        .padding(24)
        .background(Color(white: 0.06))
    }
}

#Preview("ConsoleDrawer 11-inch") {
    ConsoleCases()
        .frame(width: 1194, height: 834)
}

#Preview("ConsoleDrawer 13-inch") {
    ConsoleCases()
        .frame(width: 1366, height: 1024)
}

#Preview("ConsoleDrawer empty") {
    ConsoleDrawer(console: ConsoleBuffer(),
                  onSubscribe: { _ in },
                  onLoadOlder: { _ in },
                  initialLevel: .full)
        .frame(width: 1033, height: 448)
        .background(Color(white: 0.06))
        .preferredColorScheme(.dark)
}
