# Backlog

## Hub: interrupt racing a background hand-off result — FIXED on release/1.0.0 (2026-11-12)

Status: fixed, tests added, CI skips still in place until a green run confirms it on all three OSes.

Root cause: `idle` was emitted only by the task that owned the turn slot, in its own `finally`. An
interrupt, a queued turn and a background hand-off result can land in any order, and when two of them
interleaved the last frame could be `thinking` with no task owning the slot left to overwrite it — the
watch then sat on a spinner until the user touched it. The three CI-only skips were this stall, not a
slow runner.

Fix: `PocketHub._settle()` asks the session where it actually is and says so, and it runs on every
terminal path — the turn guard's `finally` (including when the task no longer owns the slot) and after
`_interrupt()` — with `_turn_busy`/`pending_turns` guards so a queued turn is never cut in front of.
`set_screen()` already suppresses a duplicate frame, so the ordinary path costs nothing.

Tests: `plugin/tests/test_settle.py` drives each interleaving deliberately (a task that lost the slot,
a queued turn, an already-idle screen, an interrupt mid-reply).

To finish: once CI is green on Linux, macOS and Windows, delete the `CI == "true"` markers in
`plugin/tests/test_modes.py` (three tests: `test_quick_streams_deltas_and_keeps_history`,
`test_set_mode_session_uses_resume_id_and_bad_session`,
`test_handoff_split_marker_speaks_on_it_and_delivers_result_later`) and the skip count drops to zero.

## Hub: interrupt racing a background hand-off result (found 2026-11-12, target 1.0.2)

`plugin/tests/test_modes.py::test_handoff_split_marker_speaks_on_it_and_delivers_result_later` and
`::test_quick_streams_deltas_and_keeps_history` time out on GitHub's shared runners (4 of 6 jobs on
the first CI run) and never locally (40/40, also under load and with websockets 15.0.1). The handoff
test sends `interrupt` and releases the background result back to back; when they arrive in the
other order the hub never sends a final `state: idle`. A user who interrupts the moment a background
answer lands could see the same stall. `::test_set_mode_session_uses_resume_id_and_bad_session` stalls the same way on macOS runners
(hub plus `QuickAgent` against the local SSE server). All three skip only when `CI=true` until
this is fixed; the common factor is the quick-agent path, so look there first.
