# Hail Hermes — 60-second watch test checklist

Do the six checks in order, on the physical watch. Tick PASS or write down what you saw. Total time about one minute.

**1. Rain answer without waking the agent**
Do: say "will it rain" to the watch.
Pass: within a few seconds you hear one spoken sentence about rain today, and the watch never shows the agent waking up (no spinner, no "thinking" screen, no wake sound).
If this fails, note: whether you heard silence, more than one sentence, or saw the watch visibly wake the agent before answering.

**2. Switch model to Nous default**
Do: open Settings on the watch, change the model from GLM-5.3-Flash-EXL3 to the Nous default, then ask any short question.
Pass: the setting shows the Nous default as selected, and the next answer comes back and works normally.
If this fails, note: whether the setting refused to change, snapped back to the old model, or the next answer errored or hung.

**3. Switch character to Teal ring**
Do: open Settings, choose the "Teal ring" character.
Pass: the watch face changes to the teal ring right away, without leaving Settings or restarting the app.
If this fails, note: whether the face stayed the same, only changed after reopening the app, or showed the wrong design.

**4. Session picker shows folder and age**
Do: open the session picker.
Pass: the rows show a folder name and how long ago, like "Argo · 10m ago", and tapping one opens that session.
If this fails, note: whether rows were blank, missing the folder or the age, or tapping a row did nothing.

**5. Hold to talk**
Do: hold the to-talk button, speak a short question, then release.
Pass: your words are picked up, and you hear the spoken reply after you let go.
If this fails, note: whether nothing was recorded, the release did not send, or the reply never came back.

**6. Reconnect after hub restart**
Do: put the watch on the charger with the app open, kill the hub on the laptop, then start it again.
Pass: the watch reconnects by itself within 30 seconds, with no taps on the watch.
If this fails, note: how long it actually took, and whether you had to reopen the app or tap a reconnect button by hand.

## After the six

- If **1** passed but **2** did not, the weather path is fine and the model hot-swap is the problem.
- If **6** failed, nothing else matters yet: connection recovery is the judged criterion.
- Anything that needed the app closed and reopened is a defect, not a quirk. Write down which step.
