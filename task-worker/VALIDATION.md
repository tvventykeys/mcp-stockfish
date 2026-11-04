# Validation record

## Observed worker checks

Final expanded suite: **13 tests passed, zero skips, 9.152 seconds** using restored Stockfish plus deterministic fake UCI engines. Existing Home analyses were not used as test inputs.

Coverage:

- Real Stockfish searches and request/attempt/generation mismatch rejection.
- White-perspective score conversion, with deterministic Black-to-move bound reversal.
- Illegal history/null moves/roots, resource bounds, expired requests, malformed FEN including CR/LF/tab, duplicate/nonfinite JSON, strict result schema/type checks, illegal PVs, duplicate roots and pinned engine digest rejection.
- Per-user capacity contention rejected before spawning another engine.
- Cancellation before launch, cancellation during execution, SIGTERM, hard timeout and parent-loss cleanup. Fake engine heartbeats stop after cancellation/parent loss.
- Oversized engine output rejection; valid result larger than 4 KiB drained through a forced 4 KiB stdout pipe without deadlock.

## Fixed defects from the first prototype

The first version waited for child exit before draining stdout, which could deadlock on larger output; accepted raw FEN whitespace that could reach UCI; validated result shapes too loosely; and had process-spawn/parent-loss cancellation races. These are fixed in this branch. The earlier six-test result did not cover these cases.

## Scope limits

No actual Home-to-Codex end-to-end test has run. No authenticated remote transport, Home queue integration, critical verification replacement or gaming-pause synchronization is claimed. This task has no supported inbound publication capability. Supported outbound networking can support a future task-worker pull from Home, subject to reviewed Home handlers, reachability and credentials.

No Home Windows process, job, checkpoint, startup bridge, connector, hosting configuration or environment publication was changed. Tests launched only isolated task engines. The supervisor uses one thread and 64 MiB Hash per engine; this is UCI option budgeting rather than a kernel RSS/CPU quota.

The normal companion output directory cannot be created in this executor because its root filesystem is read-only. Delivery uses an isolated repository branch instead; production files/default branch are not modified by this patch.
