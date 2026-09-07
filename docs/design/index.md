# Design and Implementation Records

These documents preserve design intent and implementation context. They are grouped by maintenance purpose, not by delivery date. Most records are written in the language used by the original implementation work.

> The current code and tests are authoritative. Read [Documentation Maintenance](/contributing/documentation) before treating a proposal or dated record as a current contract.

## Architecture and long-lived contracts

- [Avatar performance module maintenance](./avatar-performance-module-maintenance)
- [Avatar tool interaction design and maintenance](./avatar-tool-interaction-design-and-maintenance)
- [Avatar tool prompt guidelines](./avatar-tool-prompt-guidelines)
- [Cat Mind state-machine rules](./cat-idle-state-machine-rules)
- [Cat idle states](./cat-idle-states-feature)
- [Deep topic hooks](./deep-topic-hooks)
- [LLM prompt budget](./llm-prompt-budget)
- [Owner voice identity enrollment and independent verification](./owner-voice-identity-enrollment-contract)
- [Owner voice identity denial recovery](./owner-voice-identity-deny-recovery-contract)
- [Owner voice identity exact-interval adjudication](./owner-voice-identity-exact-interval-contract)
- [Proactive reason-code guide](./proactive-reason-code-guide.zh-CN)
- [User activity tracker](./user-activity-tracker)
- [Voice design architecture](./voice-design-architecture)

## Implemented design records

- [ASR client phase record](./asr-client-phase1)
- [Compact chat mode](./compact-chat-mode-design)
- [Memory event journal](./memory-event-log-rfc)
- [User-driven memory evidence](./memory-evidence-rfc)
- [PNGTuber lightweight avatar](./pngtuber-lightweight-avatar-plan)
- [Translation subtitle panel](./translation-subtitle-panel-design)
- [TTS provider and voice-source unification](./tts-voice-source-unification)
- [Live2D idle motion selection and recovery](/live2d_motion_plan)
- [PNGTubeRemix layered physics compatibility](/pngtuber-remix-physics-plan)

## Product-flow and interaction records

- [Seven-day floating avatar guide](./avatar-floating-7day-complete-guide-dev)
- [Floating avatar panel functions](./avatar-floating-panel-functions)
- [Post-tutorial low-disruption chat branches](./avatar-floating-post-theater-chat-branches)
- [CAT1 Playground Drop](./cat1-playground-drop-design)
- [Focus / True-Name mode](./focus-truename-mode)
- [Memory-browser particle dissolve](./memory-browser-particle-dissolve)
- [Yui guide-system cursor hiding](./yui-guide-system-cursor-hiding)

## Security, persistence, and incident analysis

- [Local mutation endpoint authentication](./security/local-mutation-auth)
- [Steam Auto-Cloud synchronization](./cloud-save-sync-optimization-plan)
- [Telemetry distribution and Steam user ID race](./telemetry-distribution-race-impact)

New records should state whether they are a current contract, implemented record, proposal, historical snapshot, or deprecated document near the beginning.
