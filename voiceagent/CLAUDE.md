# voiceagent: project context and handoff

This file carries over everything from the chat where the project started (September 2026), so a new session in Claude Code or Cowork can continue without re-explaining. Read it fully before changing anything.

## What this is and why

Berk wants a voice assistant he can talk to any time, like a person, that also does things for him (lights, timers, later calendar, email, music). Alexa skills felt too limited. The goal is a real product, not a weekend hobby: something that could later be offered to other people.

It is his own Claude-powered assistant. The Echo Dot is used only as a Bluetooth speaker because Amazon doesn't let outside software use its mic. Mics are phones (Android) and the MacBook.

## Decisions made so far, with reasons

- Path chosen: build our own assistant in Python ("path 4"). Rejected as the center of the design: an Alexa skill (needs "Alexa, open ..." every time, roughly 8 second response limit) and Home Assistant as the core (fine as a future tool, but the brain should be ours). Gemini on the Android phone with "Hey Google" and Gemini Live is kept as a no-code option Berk can use in parallel any time; it can't have a custom wake word.
- No new hardware. Uses the existing MacBook, Android phone and Echo Dot.
- Echo Dot = speaker only, paired over Bluetooth ("Alexa, pair") to whichever device is the satellite. It keeps working as a normal Alexa in parallel.
- MacBook lid must stay open: `caffeinate` doesn't stop lid-close sleep, and Apple Silicon MacBooks disconnect the built-in mic when the lid is closed. Use `caffeinate -ims` so the screen can sleep while the system stays awake.
- Architecture: brain and satellites are split (done in v0.2). The brain runs wake word, end-of-speech detection, Whisper, the agent, memory and tools. Satellites stream mic audio over a websocket and speak the returned sentences with their own local TTS. Berk explicitly doesn't want to depend on the Mac for mics, which is why phones are satellites.
- The brain still needs one always-on machine. Today that's the Mac; later a mini PC or Raspberry Pi 5. A cloud brain is possible, but Govee LAN control then needs Govee's cloud API or satellites that run local tools.
- Model access:
  - `llm.provider: anthropic` uses an API key. Default. Required for anything other people use.
  - `llm.provider: claude_cli` uses Berk's own Claude Pro/Max subscription for his personal use only, by running the official `claude` CLI headlessly (`claude -p`) on his own machine. We never extract or reuse the CLI's OAuth token. Research in the chat confirmed Anthropic banned third-party use of subscription OAuth tokens (ToS change Feb 2026, enforcement April 4, 2026), while running the official CLI on your own machine is ordinary use.
  - Gemini: Google shut down subscription logins in Gemini CLI for consumer tiers (June 18, 2026) and bans reusing its OAuth in third-party tools. Gemini is API key only.
  - For a future product: API keys held server-side, or a user-pays "connect your account" flow via OpenRouter OAuth (proposed, not built, Berk hasn't confirmed yet).
- Lights: Govee (controlled over LAN, "LAN Control" must be enabled per device in the Govee Home app) and Lepro. Lepro support is pending: it depends on whether his Lepro lights use Smart Life/Tuya (then the Tuya route works) or Lepro's own LampUX app (no official integration). Ask him which app.
- Multilingual: Whisper detects the language per utterance, the model replies in that language, TTS switches voice (en Samantha, tr Yelda, de Anna on macOS). Berk speaks Turkish, English and German.

## Current state (v0.2)

15 unit and integration tests pass: `python -m unittest discover -s tests`

Tested: agent tool loop (fake Anthropic client), memory, sentence streaming, Govee packet format, timers, VAD collector state machine, full websocket protocol against a real server, the real satellite client end to end (scripted mic, print TTS), and the claude_cli provider end to end through the MCP bridge (fake `claude` binary that spawns the bridge and calls a tool).

Not yet tested on real hardware, expect tuning:
- mic capture, openWakeWord and faster-whisper on the Mac
- macOS `say` voices, especially Yelda for Turkish (must be installed in System Settings, Accessibility, Spoken Content)
- Android mic capture in Termux via `pulseaudio --load=module-sles-source` + `parec`. This is the least certain piece; it depends on Android granting Termux mic permission. The native app replaces it.
- the Android app on a real phone: AudioRecord levels with VOICE_RECOGNITION, TextToSpeech voices for tr and de (Google TTS may need the voice data downloaded), whether the mic keeps running with the screen off for hours on his phone's battery management, and routing TTS to the Echo over Bluetooth
- the real `claude` CLI flags: `--include-partial-messages`, `--tools ""`, `--system-prompt`, `--strict-mcp-config`, `--allowedTools mcp__voiceagent`. If the installed version rejects one, update Claude Code or adjust `llm.cli_args`.
- Govee LAN discovery on his network

The code lives on GitHub in `thebca/claude-for-alexa`, in the `voiceagent/` folder. The older Alexa to OpenClaw bridge (Node, `server.js`, `skill-package/`) stays at the repo root. Run the tests from `voiceagent/`.

The Android satellite app is in `voiceagent/android/` (Kotlin, no AndroidX, only OkHttp). `.github/workflows/android.yml` at the repo root runs its unit tests, builds a release APK and publishes it as a GitHub release on every push that touches it. Main-branch builds are normal releases, other branches are pre-releases. Without the signing secrets (see `android/README.md`) builds are signed with a throwaway key. Its first real Android build happens in CI: cloud Claude Code sessions can't reach dl.google.com, so AGP and the SDK aren't available there. In that setup the app code was checked with a plain JVM Gradle project that compiles `app/src/main/java` against `org.robolectric:android-all` from Maven Central (with a stub R class) and runs the unit tests, plus a test running `SatelliteClient` against the real Python `Brain` with fake wake word, VAD, STT and agent. Both passed.

## File map

```
voiceagent/
  __main__.py     CLI: serve, satellite, run (all-in-one), chat (text), govee-discover, audio-devices
  config.py       DEFAULTS dict deep-merged with config.yaml; Config gives attribute access
  server.py       brain as websocket server; Session state machine per satellite (idle, listening, thinking, speaking); protocol documented at top of file
  satellite.py    thin client: SoundDeviceMic or CommandMic (parec/arecord), Voice (TTS queue that mutes the mic while speaking plus a tail), auto-reconnect with backoff, token auth
  assistant.py    all-in-one single-machine mode (local mic), also holds build_tools and NOISE/_normalize used by server
  listening.py    UtteranceCollector: end-of-speech detection as a pure streaming state machine counting audio time (testable, network-safe)
  audio.py        MicStream, WakeWord (openWakeWord, onnx), SpeechToText (faster-whisper), record_utterance
  tts.py          SayTTS (macOS), PiperTTS, TermuxTTS (Android), PrintTTS, Speaker queue, chime; no numpy so phones stay light
  agent.py        build_system_prompt, SentenceSplitter, clean_for_speech, Agent (Anthropic API, streaming, tool loop), make_agent (provider switch)
  claude_cli.py   ClaudeCLIAgent (runs `claude -p` with stream-json, strips ANTHROPIC_API_KEY from env so the subscription is used) and ToolRelay (localhost HTTP, random token)
  mcp_bridge.py   stdlib-only stdio MCP server the CLI launches; forwards tools/list and tools/call to the ToolRelay
  memory.py       SQLite: turns (short-term, expire after history_max_age_hours) and facts (long-term until forgotten)
  tools/__init__.py  ToolRegistry: register(name, description, schema) decorator, run() never raises
  tools/core.py   remember, forget, set_timer (announces through ctx["announce"], broadcast to all satellites in server mode)
  tools/govee.py  LAN API: discover (multicast 239.255.255.250:4001, replies on 4002), control on 4003, control_lights tool
  tools/roborock.py  Roborock vacuum via python-roborock on its own event loop thread; roborock-login saves ~/.voiceagent/roborock.json
  admin.py        admin web UI: stdlib HTTP server thread, JSON API, log buffer; admin.html is the single page
tests/            test_core.py, test_network.py, test_claude_cli.py, fake_claude.py
README.md         setup and usage, including Android/Termux steps
ARCHITECTURE.md   product thinking, latency budget, protocol trade-offs, roadmap, open product questions
config.example.yaml
requirements.txt
android/          native satellite app (see android/README.md)
  app/src/main/java/com/voiceagent/satellite/
    Protocol.kt         message parsing and building, constants (no Android APIs)
    Voice.kt            speech queue that mutes the mic while speaking plus a tail (no Android APIs)
    SatelliteClient.kt  OkHttp websocket client, mic pump, reconnect with backoff, stops on 4001 (no Android APIs)
    StatusBus.kt        status line from the service to the screen (no Android APIs)
    AndroidAudio.kt     AndroidMic (AudioRecord 16 kHz), AndroidTts (TextToSpeech per language), ToneChime
    SatelliteService.kt foreground service (microphone type), wake and Wi-Fi locks, notification with Stop
    MainActivity.kt     settings screen built in code: brain URL, token, room id, chime, start/stop, battery optimization
    Settings.kt         SharedPreferences
  app/src/test/   ProtocolTest, VoiceTest, SatelliteClientTest (MockWebServer as the brain)
```

Websocket protocol (details in server.py): satellite sends hello with id and token, then binary 16 kHz mono int16 PCM frames of 80 ms, and `speech_done` after it finishes speaking a turn. Brain sends `wake`, `say` (text + lang), `turn_end`, `announce`, `conversation_end`, and `sleep` / `awake` when sleep mode changes. Wake word currently runs on the brain, so satellites stream continuously (about 32 KB/s); moving wake word onto the device later needs no protocol change.

## Next steps, in the order agreed

1. Done: pushed to GitHub as `voiceagent/` in `thebca/claude-for-alexa`.
2. Native Android satellite app in Kotlin, installed by sideloading, no Play Store and no local build. Built, waiting for its first CI build and a test on Berk's phone. Berk still needs to add the signing secrets once (commands in android/README.md). What it was built to:
   - GitHub Actions workflow builds a debug/release APK on every push and attaches it to a GitHub release
   - Berk installs it by downloading the APK on the phone and allowing "install unknown apps" once; Obtainium can auto-update from the private repo
   - app contents: settings screen (brain URL, token, room id), foreground service with microphone type and persistent notification (required by Android for always-on mic), AudioRecord at 16 kHz mono, websocket client speaking the same protocol, Android TextToSpeech per language, reconnect with backoff
   - later in the app: on-device wake word (openWakeWord ONNX via ONNX Runtime Mobile) so the phone only streams after the wake word
3. OpenRouter "connect your account" OAuth (PKCE) as a provider, if Berk confirms. This is the product path for other users.
4. Lepro: Berk's lights use Lepro's own app (LampUX), not Tuya. Research (September 2026): no local control at all (every TCP port closed on both bulbs, 192.168.178.20 and .41, Espressif ESP8684). The only route is Lepro's cloud: HTTPS login at api-eu-iot.lepro.com, then MQTT over mutual TLS with a client key shipped inside the Lepro app. The only open client, github.com/Sanji78/lepro_led, is "All Rights Reserved", so nothing may be copied from it, including its bundled key. Options still to decide with Berk: our own client from the documented protocol facts with a key he extracts himself, going through Alexa (the Echo already controls them), or Matter if his bulbs support it.
5. Roadmap v0.2 remainder: barge-in with echo cancellation, streaming TTS, fast local path for simple commands like "lights off" that skips the LLM (matters most in claude_cli mode where replies start 1 to 3 s later), prompt caching, Gemini API provider, Home Assistant tool.
6. v0.3: MCP servers work in claude_cli mode through `llm.mcp_servers` (passed to the CLI); API mode still needs an MCP client. The admin UI exists (see below); history and latency charts are not in it yet.

Open product questions (not decided): first customer (Home Assistant tinkerers vs ordinary people), bring-your-own-key vs subscription, product name.

## Added in v0.3 (September 25, 2026)

- Sleep mode: "Jarvis, go to sleep" / "uyu" / "schlaf" makes the brain ignore everything, including the wake word, until "hey Jarvis, wake up" / "uyan" / "wach auf". Brain-wide, phrases in `assistant.sleep_phrases` and `assistant.wake_phrases`, filler words and the assistant name are ignored when matching. Only Whisper runs while asleep, and only after a wake word.
- Latency in claude_cli mode, measured on Berk's M3 Air: from end of speech to first word went from about 3.3 s to 2.0 s. The CLI process is prewarmed at the wake word and at each follow-up, runs with `--setting-sources ""` from ~/.voiceagent (so Berk's own hooks, plugins and this CLAUDE.md don't load), and with `MAX_THINKING_TOKENS=0` (Haiku's extended thinking cost about 0.7 s). The rest is Whisper base (about 0.6 s) and model time to first token (about 0.9 s). Whisper small was tested: better Turkish but 2.9 s per sentence on this Mac, so base stays.
- Whisper auto-detect is limited to `stt.languages` (en, tr, de); a Turkish sentence was detected as Russian before. Segments that look like noise (no_speech_prob > 0.6 and avg_logprob < -1) are dropped.
- Web search on by default (`llm.web_search`): WebSearch/WebFetch in CLI mode, the web_search server tool in API mode.
- Roborock vacuum (`tools/roborock.py`, python-roborock 7.9): `python -m voiceagent roborock-login` once (email code), then a `vacuum` tool (start, stop, pause, dock, status, find, clean_rooms by room name). Local network first, Roborock cloud as fallback. Unit tested with a fake device only; not yet tried on Berk's vacuum (Roborock at 192.168.178.26).
- Admin web UI (`admin.py` + `admin.html`) at http://<brain>:8766 with the same token: sleep toggle, satellites, light and vacuum cards, forms for every tool, typed chat, announcements, memory, config.yaml editor with restart, live log.
- Android app: prefers Google's TTS engine, picks the best installed voice per language, and has a voice picker per language with Test buttons, a speed setting and a "Download voices" button. Shows "Sleeping" in the notification.
- Network scan (September 25, 2026) on 192.168.178.0/24: Roborock .26, likely Govee .34 (LAN Control was off, discovery got no answer), two Lepro bulbs .20 and .41, Echo .32.

## How to work in this repo

- Python 3.11 recommended on the Mac. Satellite code must stay light: only websockets and pyyaml, plus sounddevice where available. No numpy in satellite or tts paths.
- Every new capability is a function in `tools/` registered with a JSON schema. Tools get a `ctx` dict (memory, announce) and must never raise; ToolRegistry.run converts exceptions to "error: ..." strings.
- Keep providers behind `respond(text, on_sentence, lang) -> str` and create them via `make_agent`.
- Add tests for anything that can run without audio hardware or API keys. Use fakes like the ones in tests/.
- Never implement anything that extracts or reuses Claude Code or Gemini CLI OAuth tokens. Subscription use stays personal and goes only through the official CLI.

## Berk's preferences for writing

For prose and documents (not code or code comments): natural plain style, no em dashes, no rule-of-three structures, no overuse of bold or headers, no filler or hedging, no sycophantic openers, no signposting like "let's dive in", no inflated language. Replies on his phone should be short and lead with the answer.
