# Android phone client

Open this directory in Android Studio or run `./gradlew :app:assembleDebug :app:testDebugUnitTest` with JDK 17 and Android SDK 35. The emulator default hub is `10.0.2.2:8767`; change it in Debug settings for a physical phone. Compare the displayed pairing code with the hub and confirm it. The UUID and token are stored in Preferences DataStore. Changing host or resetting the UUID discards the token.

Hold the talk control while speaking; release it to send the utterance. Microphone and Android 13+ notification permissions are requested on launch. The foreground service maintains the connection while Android allows it; Android 15 limits background data-sync foreground service time. Open the app again if the system stops it. Reconnection uses exponential backoff and WebSocket pings. Hub traffic is unencrypted by default; use a `wss://` URL when available.

The share sheet sends shared text or an image content URI as text when the connection is ready. The hub must have a way to access that URI; it is not an image upload. Transcripts are retained in memory only. Pairing tokens are stored in app-private preferences, not hardware-backed storage.

The workflow template is at `.github/workflows/android.yml` within this component. GitHub Actions only discovers workflows in the repository-root `.github/workflows/`; copy it there during integration. It is kept here because this component's builder is restricted to `android/`.
