# When the watch says something failed

_What each failure message means and the shortest path back to a working turn._

Applies to Hail Hermes 1.0.0 · revised 2026-11-11

All of these start the same way: look at the last few lines of the turn log, which records one
JSON line per question and answer.

```
tail -5 %LOCALAPPDATA%\hermes\hail-hermes\turns.jsonl
tail -40 %LOCALAPPDATA%\hermes\hail-hermes\hub.log
```

## "Hermes failed · tap to retry"

The agent could not answer. In order of likelihood:

1. **The session is pinned to a model its provider no longer serves.** Most common cause: the
   watch is pinned to an old session that was created while the provider setting was wrong, so it
   keeps asking that provider for a model it does not have. The hub now notices this and answers in
   a fresh session instead. To undo the pin by hand, use the mode pill on the watch and pick
   **Quick ask**.
2. **The session was deleted or moved on the computer.** Same self-heal applies.
3. **The model name is wrong for the provider.** Check with
   `hermes hail settings` and compare with the list the pane offers. A bare provider with no model
   is not valid — Hermes keeps the configured model name when only the provider changes.
4. **The provider is refusing.** `agent_failed` plus a line in `hub.log` starting with `turn failed
   for ...:` now carries the child's own words, e.g. a 401 or a 404 from the provider.

## "Could not hear you · tap to retry"

Speech-to-text produced nothing. Usually the watch was too far from the mouth, or the microphone was
covered. If it repeats every time, check `stt result:` lines in `hub.log` — a missing line means the
audio never arrived.

## "Speech failed · tap to retry"

The answer was produced but could not be turned into audio. Check that ffmpeg is installed
(`hermes pm install --tools-only`) and that the TTS backend is set (`hermes hail settings tts`).

## The watch shows "Reconnecting…" for a long time

That is not an error, and it no longer asks you to tap anything: the watch retries on its own and
watches for the hub's discovery beacon. If it stays that way for more than a minute, the hub is
probably not running. Restart it:

```
hermes hail restart
```

or double-click **Restart Hail Hub** on the desktop.

## Nothing works and you want a clean slate

```
hermes hail devices            # who is paired
hermes hail revoke <device-id> # forget one
hermes hail restart
```

Revoking means pairing again, which is a 4-digit code and one approval.

---

Back to [docs/README.md](README.md).
