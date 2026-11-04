# Home connection: current blocker and practical next implementation

## Verified in this task

- The managed Codex task is a separate execution environment.
- Its current status has no Home credential binding or outbound identity.
- Current policy has no configured VPN.
- The runtime's supported TCP/VPN forwarding documentation explicitly excludes inbound connections. The forwarding examples connect FROM the task to a private service, not from Windows into the task.
- No supported public port publication tool is exposed to this task. Starting a local HTTP server would not establish an authenticated Windows-reachable endpoint.

Publishing/restoring this environment does not turn it into a continuously available service. No direct Home-to-Codex connection has been tested or established.

## Recommended connection direction for a future Home adapter

Use outbound pull from the task worker to a narrowly scoped authenticated endpoint owned by Home. Home retains one queue, its existing local engine lane, a separate one-slot remote capacity ledger, its Turtle/priority/gaming policies, and sole ownership of checkpoint/result writes.

Proposed lifecycle (NOT implemented):

1. Home leases an eligible independent request to an authenticated worker principal and persists immutable request/attempt/generation plus expiry before returning it.
2. The active task retrieves that lease through the configured HTTPS proxy. It creates one isolated bounded engine, never mounting Home storage.
3. While searching, it renews only within an immutable hard execution deadline and polls cancellation. A lost connection never extends that deadline.
4. Home invalidates the attempt generation when gaming policy, cancellation or reassignment requires it. A remote review running separately must not trigger unnecessary local Turtle suspension.
5. Worker submits request-bound evidence. Home reconstructs position/history, validates schema/settings/provenance/legal PVs and required evidence, then commits once through its canonical writer.
6. Home may fallback locally only after cancellation acknowledgement or lease expiry. Late replies are rejected even if structurally valid.

This requires NEW source-level queue/lease/result handlers in Home and a supported route to them. The current Home connector tool list does not expose these operations. The Windows startup bridge is not the place to add them.

## What is still needed before implementing or enabling that connection

- Current Home server source as files (this task has sanitized source reviews, not the actual Go source tree).
- A supported existing HTTPS route to the new Home queue handlers, or reviewed environment VPN configuration to reach Home privately.
- A dedicated least-privilege worker authentication mechanism; do not send account tokens in chat.
- Explicit scheduler integration and end-to-end tests with NEW disposable jobs, including default and strict gaming policy, Turtle fairness, cancellation, task disappearance, duplicate responses and local fallback.

No route, new endpoint, tunnel, credential or Home configuration was created. No additional Windows process or startup task is required by this design: Home handlers live inside its existing service. The design cannot be enabled solely by editing this worker.

## Sources

- OpenAI, Using Codex Cloud: https://help.openai.com/en/articles/20001545-using-codex-cloud
- OpenAI, cloud environment configuration: https://learn.chatgpt.com/docs/environments/cloud-environments
- Runtime Cloud Environment skill, references/vpn.md: outbound HTTP CONNECT forwarding; limits exclude inbound connections.

Documentation establishes the supported direction, not successful connectivity to this user's Home. No Home endpoint was contacted during this fix.
