import SwiftUI
import UIKit

struct ConsoleDrawer: View {
    enum Level: CaseIterable {
        case collapsed, half, full
    }

    let console: ConsoleBuffer
    let onSubscribe: (Bool) -> Void
    let onLoadOlder: (Int) -> Void

    @State private var level: Level
    @State private var hideResiduals = true
    @State private var filter = ""
    @State private var wrap = false
    @State private var atBottom = true
    @State private var unseen = 0
    @State private var position: Int?
    @State private var requestedTop: Int?

    private static let topID = Int.min
    private static let bottomID = Int.max

    init(console: ConsoleBuffer,
         onSubscribe: @escaping (Bool) -> Void,
         onLoadOlder: @escaping (Int) -> Void,
         initialLevel: Level = .collapsed) {
        self.console = console
        self.onSubscribe = onSubscribe
        self.onLoadOlder = onLoadOlder
        _level = State(initialValue: initialLevel)
    }

    var body: some View {
        GeometryReader { proxy in
            VStack(spacing: 0) {
                Spacer(minLength: 0)
                panel(height: panelHeight(in: proxy.size.height))
            }
            .frame(width: proxy.size.width, height: proxy.size.height, alignment: .bottom)
        }
        .animation(.snappy(duration: 0.25), value: level)
        .onChange(of: level) { _, new in
            onSubscribe(new != .collapsed)
        }
    }

    private func panelHeight(in column: CGFloat) -> CGFloat {
        switch level {
        case .collapsed: return 28
        case .half: return 0.45 * column
        case .full: return column
        }
    }

    private func panel(height: CGFloat) -> some View {
        VStack(spacing: 0) {
            handle
            if level != .collapsed {
                controls
                Divider()
                content
            }
        }
        .frame(height: height)
        .background(Color(white: 0.11))
        .clipShape(RoundedRectangle(cornerRadius: 12))
    }

    private var handle: some View {
        HStack(spacing: 10) {
            Capsule()
                .fill(.secondary)
                .frame(width: 36, height: 4)
            if level == .collapsed {
                Text(ticker)
                    .font(.system(size: 12, design: .monospaced))
                    .foregroundStyle(Self.color(newest?.kind ?? "n"))
                    .lineLimit(1)
                    .truncationMode(.tail)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 12)
        .frame(height: 28)
        .frame(maxWidth: .infinity)
        .contentShape(Rectangle())
        .onTapGesture {
            shift(1)
        }
        .gesture(
            DragGesture(minimumDistance: 12)
                .onEnded { value in
                    if value.translation.height < -20 {
                        shift(1)
                    } else if value.translation.height > 20 {
                        shift(-1)
                    }
                }
        )
    }

    private var controls: some View {
        HStack(spacing: 12) {
            Toggle("Hide residual rows", isOn: $hideResiduals)
                .fixedSize()
            TextField("Filter", text: $filter)
                .textFieldStyle(.roundedBorder)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
            Toggle("Wrap", isOn: $wrap)
                .toggleStyle(.button)
                .fixedSize()
        }
        .controlSize(.small)
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
    }

    @ViewBuilder
    private var content: some View {
        if console.lines.isEmpty {
            placeholder("No console output yet")
        } else if visible.isEmpty {
            placeholder("No matching lines")
        } else {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 0) {
                        Color.clear
                            .frame(height: 1)
                            .id(Self.topID)
                            .onAppear(perform: requestOlder)
                        ForEach(visible) { line in
                            row(line)
                                .id(line.id)
                        }
                        Color.clear
                            .frame(height: 1)
                            .id(Self.bottomID)
                            .onAppear {
                                atBottom = true
                                unseen = 0
                            }
                            .onDisappear {
                                atBottom = false
                            }
                    }
                    .padding(.horizontal, 12)
                }
                .defaultScrollAnchor(.bottom)
                .scrollPosition(id: $position)
                .onChange(of: console.lines.last?.id) { old, new in
                    guard let new else { return }
                    let added = max(0, new - (old ?? new))
                    if atBottom {
                        var transaction = Transaction()
                        transaction.disablesAnimations = added > 50
                        withTransaction(transaction) {
                            proxy.scrollTo(Self.bottomID, anchor: .bottom)
                        }
                    } else {
                        unseen += added
                    }
                }
                .overlay(alignment: .bottomTrailing) {
                    if !atBottom {
                        Button {
                            unseen = 0
                            withAnimation {
                                proxy.scrollTo(Self.bottomID, anchor: .bottom)
                            }
                        } label: {
                            Text("Live ↓ \(unseen) new")
                                .font(.system(size: 12, weight: .semibold))
                                .padding(.horizontal, 12)
                                .padding(.vertical, 6)
                                .background(.thinMaterial, in: Capsule())
                        }
                        .padding(12)
                    }
                }
            }
        }
    }

    private func placeholder(_ text: String) -> some View {
        Text(text)
            .font(.system(size: 13))
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func row(_ line: ConsoleLine) -> some View {
        Text(line.text)
            .font(.system(size: 12, design: .monospaced).weight(line.kind == "h" ? .bold : .regular))
            .foregroundStyle(Self.color(line.kind))
            .lineLimit(wrap ? nil : 1)
            .frame(maxWidth: .infinity, alignment: .leading)
            .onLongPressGesture {
                UIPasteboard.general.string = line.text
            }
    }

    private var visible: [ConsoleLine] {
        console.lines.filter { line in
            (!hideResiduals || line.kind != "r")
                && (filter.isEmpty || line.text.localizedCaseInsensitiveContains(filter))
        }
    }

    private var newest: ConsoleLine? {
        console.lines.last { $0.kind != "r" }
    }

    private var ticker: String {
        newest?.text ?? "No console output yet"
    }

    private func shift(_ delta: Int) {
        let all = Level.allCases
        let index = all.firstIndex(of: level) ?? 0
        level = all[min(all.count - 1, max(0, index + delta))]
    }

    private func requestOlder() {
        guard let first = console.lines.first?.id, first != requestedTop else { return }
        requestedTop = first
        onLoadOlder(first)
    }

    private static func color(_ kind: Character) -> Color {
        switch kind {
        case "e": return .red
        case "w": return Color(red: 1, green: 0.75, blue: 0)
        case "s": return .purple
        case "h": return .secondary
        case "r": return Color(white: 0.45)
        default: return .primary
        }
    }
}
