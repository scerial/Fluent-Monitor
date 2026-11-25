import SwiftUI

private struct ConnectCases: View {
    var body: some View {
        HStack(spacing: 24) {
            ConnectSheet(servers: SampleData.servers, onConnect: { _ in }, initial: nil,
                         discovery: .found)
            ConnectSheet(servers: [], onConnect: { _ in }, initial: nil,
                         discovery: .searching)
            ConnectSheet(servers: [], onConnect: { _ in }, initial: nil,
                         discovery: .notFound)
            ConnectSheet(servers: SampleData.servers, onConnect: { _ in }, initial: SampleData.endpoint,
                         discovery: .found, tokenRejected: true)
        }
        .preferredColorScheme(.dark)
    }
}

private struct ConnectStates: View {
    var body: some View {
        VStack(spacing: 24) {
            ConnectSheet(servers: SampleData.servers, onConnect: { _ in }, initial: nil,
                         discovery: .found)
            ConnectSheet(servers: [], onConnect: { _ in }, initial: nil,
                         discovery: .denied)
        }
        .preferredColorScheme(.dark)
    }
}

#Preview("ConnectSheet states 11-inch") {
    ConnectCases()
        .frame(width: 1194, height: 834)
}

#Preview("ConnectSheet states 13-inch") {
    ConnectCases()
        .frame(width: 1366, height: 1024)
}

#Preview("ConnectSheet denied and found 11-inch") {
    ConnectStates()
        .frame(width: 1194, height: 834)
}
