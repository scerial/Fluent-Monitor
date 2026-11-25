import SwiftUI
import UIKit

struct SceneProbe: UIViewRepresentable {
    @Binding var state: SceneState

    func makeUIView(context: Context) -> ProbeView {
        let view = ProbeView()
        let binding = $state
        view.onChange = { newState in
            DispatchQueue.main.async {
                if binding.wrappedValue != newState {
                    binding.wrappedValue = newState
                }
            }
        }
        return view
    }

    func updateUIView(_ uiView: ProbeView, context: Context) {}

    final class ProbeView: UIView {
        var onChange: ((SceneState) -> Void)?

        override func layoutSubviews() {
            super.layoutSubviews()
            report()
        }

        override func didMoveToWindow() {
            super.didMoveToWindow()
            report()
        }

        private func report() {
            guard let window, let scene = window.windowScene else { return }
            let full = window.bounds == scene.coordinateSpace.bounds
            onChange?(SceneState(isFullScreen: full, orientation: scene.interfaceOrientation))
        }
    }
}
