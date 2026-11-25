import SwiftUI

struct HeaderBand: View {
    let session: Session?
    let connection: Connection
    let lastIteration: Date?

    private static let outline = Color.white.opacity(0.25)

    var body: some View {
        HStack(spacing: 10) {
            Text(session == nil ? "No session" : Self.caseTitle(session?.caseName))
                .font(.system(size: 11.5, weight: .semibold))
                .lineLimit(1)
                .truncationMode(.tail)
            if let session {
                Text(session.mode == .steady ? "Steady" : "Transient")
                    .font(.system(size: 11.5))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            statusView
                .fixedSize()
            Spacer(minLength: 8)
            TimelineView(.periodic(from: .now, by: 1)) { context in
                Text(Self.age(since: lastIteration, now: context.date))
                    .font(.system(size: 11.5).monospacedDigit())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .fixedSize()
            }
            .layoutPriority(1)
        }
        .padding(.horizontal, 12)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .overlay(
            RoundedRectangle(cornerRadius: 6)
                .strokeBorder(Self.outline, lineWidth: 1.25)
        )
    }

    private var statusView: some View {
        let state = status
        return HStack(spacing: 6) {
            Circle()
                .fill(state.color)
                .frame(width: 8, height: 8)
            Text(state.word)
                .font(.system(size: 11.5))
                .lineLimit(1)
        }
    }

    private var status: (word: String, color: Color) {
        if connection == .offline {
            return ("Disconnected", .gray)
        }
        guard let session else {
            return ("Idle", .gray)
        }
        switch session.status {
        case .running: return ("Running", Palette.cool)
        case .converged: return ("Converged", .green)
        case .idle: return ("Idle", .gray)
        case .problem: return ("Problem", .red)
        }
    }

    static func caseTitle(_ name: String?) -> String {
        guard let name, !name.isEmpty else { return "Unnamed" }
        return name.caseInsensitiveCompare("FFF") == .orderedSame ? "Unnamed" : name
    }

    static func age(since last: Date?, now: Date) -> String {
        guard let last else { return "—" }
        let s = now.timeIntervalSince(last)
        if s < 0 { return "0 s" }
        if s < 10 {
            let tenths = (s * 10).rounded() / 10
            return "\(tenths) s ago"
        }
        if s < 60 { return "\(Int(s)) s ago" }
        if s < 3600 { return "\(Int(s / 60)) min ago" }
        let minutes = Int(s / 60)
        let mm = minutes % 60
        let padded = mm < 10 ? "0\(mm)" : "\(mm)"
        return "\(minutes / 60) h \(padded) min ago"
    }
}
