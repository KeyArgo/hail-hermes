# Hermes Pocket — WearOS Watch Client (`wear/`)

Standalone WearOS companion app for Hermes Pocket implementing Protocol v0.1 (`protocol/PROTOCOL.md`).

## Architecture & Design Decisions

- **App style**: Standalone WearOS application (`com.google.android.wearable.standalone = true`, `minSdk = 30`, `targetSdk = 35`) using **Compose for Wear OS** (`ScalingLazyColumn`, `Scaffold`, `TimeText`, `Vignette`, `PositionIndicator`) and a dedicated foreground service (`PocketWearService`) plus a Wear **TileService** (`PocketTileService`).
- **Direct WiFi Hub Connection**: Connects directly to `ws://<host>:8767/pocket` via OkHttp WebSocket without a phone intermediary. Includes 25s heartbeat pings, exponential backoff reconnects (`1s` up to `32s`), and `Preferences DataStore` persistence for `device_id`, `host`, and pairing `token`.
- **Mascot Face (`MascotCanvas.kt`)**:
  - Automatically loads 1-bit PNG frames from `src/main/assets/mascot/*.png` (`idle_1.png`, `idle_2.png`, `blink.png`, `talk_1..4.png`) if present.
  - Falls back to a procedural Compose Canvas renderer supporting all 4 protocol states:
    - `idle`: open visor eyes with periodic blink animation.
    - `listening`: concentric expanding sonar rings.
    - `thinking`: animated sine-wave bouncing dots.
    - `speaking`: animated 5-bar voice waveform mouth.
- **Push-to-Talk & Audio Path**:
  - Center hold-to-talk button captures 16 kHz mono `pcm16le` audio via `AudioRecord` in 640-byte (20 ms) binary frames bracketed by `audio_start` and `audio_end`.
  - Streams incoming hub TTS PCM frames into `AudioTrack` while in the `speaking` state.
- **Haptics & Notifications**:
  - Triggers watch vibration patterns (`VibratorManager` / `VibrationEffect`) on `tool_event` (`device_haptic`) and `notify` frames.
  - Posts high-priority system notifications when `notify` messages arrive.
- **Wear Tile (`PocketTileService.kt`)**:
  - Displays live connection status, current mascot state, and the latest transcript line, tapping through to `MainActivity`.

## Building & Testing

With JDK 17 and Android SDK 35 installed:

```bash
cd wear
./gradlew :app:assembleDebug :app:testDebugUnitTest
```

CI runs automatically via `.github/workflows/wear.yml`.
