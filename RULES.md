# Fluent Monitor: working rules

Project context: FluentMon, a Windows Python server and SwiftUI iPad client that monitor ANSYS Fluent solver runs.

## Model workflow

Work in three stages, each handled by a different model tier:

1. **Architecture:** plan large architectures with a reasoning model such as Opus.
2. **Refinement:** refine and revise the plan with a mid-tier model such as Sonnet.
3. **Implementation:** implement with a lightweight model such as Haiku, working from detailed plans and instructions.

## Shared rules

- Do all planning and revision in the plan document only. Do not create separate documents, side notes or chat-only versions of the plan. The one exception is benchmark revision points, which go in the benchmark document.
- If the request clearly fits one stage or the discussion state, name it in one line and proceed.
- If you are not sure which stage or state the request belongs to, ask the user to define the scope of work before starting. Do not guess.
- Mark every value in the plan as either **decided** or **assumed**. Assumed values carry a short note on how they will be confirmed.
- Never change a fixed interface (wire protocol messages, config keys, file layout, public function signatures) silently. A change goes back to the stage that owns it and is recorded in a changelog section at the end of the plan.

## Discussion state

- Discussion is a state outside the three stages, where the user and agent talk through a problem or the plan as a whole to reach a solution or improvement.
- It can be declared by the user ("let's discuss…") or inferred from the request. Infer discussion when the user asks for suggestions or recommendations ("any suggestions?", "what would you add?", "how could this be better?", "any more rules that support this?"), asks open questions ("what do you think?"), or weighs alternatives.
- When discussion is inferred, do not edit the plan, the benchmark document or these instructions. Answer in chat and propose changes there.
- When discussion is declared, edit documents or instructions only when the user asks for it within the discussion.
- Discussion ends when the user asks for an edit or names a stage. The agreed outcome is then written into the plan document by the stage that owns it.

## Architecture stage

- Define components, data flow and every fixed interface before refinement starts.
- End with an open-questions list. Leave nothing undecided without listing it there.

## Refinement stage

- Resolve each open question, or mark it deferred with a reason.
- After any change, check the whole plan for consistency: cross-references, config samples, code snippets, diagrams and the benchmark document.

## Implementation instructions (only on request)

- Write instructions for the lightweight model only when the user asks for them. Never produce them unprompted, even when the plan looks ready.
- When asked, break the requested part of the plan into tasks. Each task states the files to touch, the functions and signatures, inputs and outputs, edge cases, an acceptance test, and what is out of scope.
- Order tasks so that each one can be built and tested on its own.

## Implementation stage

- Work on one task at a time, exactly as specified.
- Do not make architectural decisions. If a task is ambiguous, contradicts the plan, or needs a design choice, stop and report the question for the refinement stage instead of guessing.
- Do not change a fixed interface, even to fix a problem; report it instead.
- A task is done only when its acceptance test passes. Report any deviation from the task text.

## Benchmarking

- Benchmarking is a minor priority for this project. Do not expand benchmark content unprompted.
- In the benchmark document, write only the points that would need further revision, and add them at the end of the document.
- Do not go into detail on benchmark points unless the user explicitly asks for detail.

## Scope of these rules

- Apply these rules to every request in this project until the user changes them.
