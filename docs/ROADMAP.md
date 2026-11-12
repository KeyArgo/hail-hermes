# Roadmap — where new ideas go

_Every raised idea, with a plain verdict before anyone builds it._

Applies to Hail Hermes 1.0.0 · revised 2026-11-11

Every idea the owner raises lands here with a plain verdict before anyone builds it.
**Possible** means it can be built with the tooling we already have. **Possible with a
catch** means the catch is written out. **Not possible** means it cannot be done that
way, and the nearest thing that can is named instead.

## Shipped (lane/test-watch, 2026-11-07)

| Item | State |
|---|---|
| Hub auto-start when the desktop pane opens | done, verified on this laptop |
| Hub voice replies (ffmpeg was missing) | done, sound confirmed on the watch |
| Watch pairing on real hardware + runbook | done |
| Deadlock that froze the watch on "Speaking…" | fixed |
| Reconnect after a foreground drop | fixed |
| Wi-Fi released mid-reply when the screen slept | fixed |
| Pairing counted as idle, so the code expired | fixed |
| Nous mascot shipped four copies of one frame | fixed (duplicates dropped, bars restored) |
| Always-visible "working" signal + Interrupt while busy | done |
| Interrupted turns explained to the agent on the next turn | done |
| Ask/answer/error log (`turns.jsonl`) | done |

## Next (small, no new decisions needed)

1. **Two Nous Research skins bundled in the APK.** *Done:* four skins (nous-white, nous-soft, nous-teal, nous-warm) ship with talk frames that move the mouth.
2. **Voice follows the character.** *Possible, already supported.* `settings.json` takes
   `voice_<character>`, so `hermes` can be a male voice and `nous` a female one, and the
   pane's Character tab already picks between them.
3. **Character picker lists skins.** *Possible.* The picker reads the character list; two
   more entries appear in it once they are registered in `Codec.VALID_CHARACTERS` and the
   plugin's `CHARACTERS`.

## Ideas — assessed

### Watch face app (Hail on the clock screen)
**Possible, but it must be a separate app, and it cannot talk to the hub.**

- Since January 2026, Wear OS requires the **Watch Face Format**: a resource-only XML
  package, `android:hasCode="false"`, built as its own AAB/APK, separate from the app
  that contains the watch's logic.
- No code runs in a watch face, so it cannot open the WebSocket, hold the session, or
  speak. It can draw the time and show **complications**.
- So the realistic version: a Hail face that shows the time plus a complication fed by
  the Hermes app ("connected", "3 waiting", last reply). The app keeps doing the talking.
- Memory caps are real: 10 MB ambient, 100 MB interactive.

### Watch face *creator* (design your own face on the watch)
**Not possible as an on-watch editor.** WFF is declarative and fixed at package time.

- Nearest workable version: a **companion designer** (desktop or phone) that composes a
  face from templates and emits a WFF package, then pushes it with Watch Face Push or
  adb. That is a real project on its own — new app, new storage, new publishing path.
- Recommendation: keep it out of the hackathon submission. Treat it as a separate
  product bet after the deadline.

### Screen-off audio for the whole answer
**Possible, unverified.** The playback foreground service exists; today's fix stops the
Wi-Fi from being released mid-reply. Needs one honest test with the screen locked.

### Phone client
**Currently broken.** `android/` still speaks protocol v1 (`v:1`, `pair_confirm`); the hub
closes it with 4000 every time. Either port it to v2 or drop it from the submission.

## Rules

1. New idea → one row here, with a verdict, before code.
2. A row is only "done" when something real proves it (a test, a screenshot, a run).
3. If the verdict is "possible with a catch", the catch is the first thing written down.

---

Back to [docs/README.md](README.md).
