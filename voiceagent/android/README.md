# Android satellite app

A native Android app that turns a phone into a satellite for the voiceagent brain. It streams the mic to the brain over the websocket protocol in `voiceagent/server.py` and speaks the replies with Android's own text to speech, in whatever language the brain says the reply is in. It replaces the Termux setup.

There is no Play Store listing and you don't need to build it yourself. GitHub Actions builds an APK on every push that touches this folder and attaches it to a release on the repo's Releases page.

## Install

1. Start the brain on the Mac with a token: `python -m voiceagent serve --token <long-secret>`. Find the Mac's LAN address in System Settings, Wi-Fi, Details.
2. On the phone, open the repo's Releases page in the browser and tap the newest `voiceagent-satellite-*.apk`. Builds from branches other than main are marked pre-release.
3. Android asks once to allow installs from the browser. Allow it and install.
4. Open Voiceagent, enter the brain address (`ws://<mac-lan-ip>:8765`), the token and a room id, then press Start. Allow the microphone and notifications.
5. Tap "Let it run in the background" and allow it. Without this, Android tends to stop the app after a while with the screen off.
6. Pair the phone with the Echo Dot ("Alexa, pair") so replies play through it.

Voices: the app uses Speech Services by Google when it is installed, which sounds far more natural than the built-in engine on many phones (Samsung especially). Under Voices you can pick a voice for English, Turkish and German, try each with Test, and set the speed. Voices marked "(online)" are Google's server voices, usually the most natural, and need internet. "Download voices" opens Google's voice downloads if a language is missing. Press Start again after changing voices.

A notification stays up while the app listens. Android requires it for an always-on mic, and it has a Stop button.

For automatic updates, install Obtainium and add the repo URL. Turn on "include prereleases" in Obtainium if you want builds from branches other than main.

## Signing, so updates install over the old version

Android only installs an update if it is signed with the same key as the installed app. Until the repo has a signing key, each build is signed with a throwaway key, and you have to uninstall the old app before installing a new build (the settings are lost too). To fix that once, on the Mac (keytool comes with Java, `brew install openjdk` if it's missing):

```bash
keytool -genkeypair -keystore voiceagent-release.jks -alias voiceagent \
    -keyalg RSA -keysize 2048 -validity 10000
gh secret set ANDROID_KEYSTORE_BASE64 --body "$(base64 -i voiceagent-release.jks)"
gh secret set ANDROID_KEYSTORE_PASSWORD
gh secret set ANDROID_KEY_ALIAS --body voiceagent
```

Keep `voiceagent-release.jks` and its password somewhere safe, outside the repo. If it is lost, the next build needs one more uninstall.

## How it works

- `SatelliteService` is a foreground service of type microphone. It holds a partial wake lock and a Wi-Fi lock so audio keeps flowing with the screen off.
- `AndroidMic` records 16 kHz mono 16-bit audio with the voice recognition source and sends 80 ms frames, the same as `satellite.py`.
- `Voice` mutes the mic while replies play, plus a 600 ms tail so a Bluetooth speaker's delayed audio isn't sent back to the brain.
- `SatelliteClient` reconnects with backoff from 1 to 30 seconds. It stops if the brain rejects the token (close code 4001).
- The wake word still runs on the brain, so the phone streams all the time (about 32 KB/s on the LAN). On-device wake word is the next step for the app.

Android 14 and newer don't let an app start the mic from the background. If the system kills the app, it won't restart on its own. Open it and press Start.

## Tests

`Protocol.kt`, `Voice.kt`, `SatelliteClient.kt` and `StatusBus.kt` use no Android APIs. The unit tests in `app/src/test` run them on a plain JVM against a local websocket server playing the brain's part. CI runs them before every build. Locally, with the Android SDK installed: `./gradlew testDebugUnitTest assembleDebug`.
