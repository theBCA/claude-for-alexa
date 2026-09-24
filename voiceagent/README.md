# voiceagent

A local-first voice assistant you can actually talk to. Custom wake word, Claude as the brain, memory that persists, and tools that control your home.

It runs as two parts. The brain (`serve`) does speech recognition, Claude, memory and tools. Satellites (`satellite`) are the mics and speakers in each room: an Android phone, a Mac, a Raspberry Pi. Any speaker works, including an Echo Dot paired over Bluetooth to a satellite.

## Setup (macOS, Python 3.11 recommended)

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
export ANTHROPIC_API_KEY=sk-ant-...
```

Pair the Echo Dot: say "Alexa, pair", then pick it in macOS Bluetooth settings and set it as the sound output. The first `run` downloads the wake word and Whisper models (a few hundred MB).

## Which Claude account pays

Two providers, set with `llm.provider` in config.yaml:

`anthropic` (default) uses an API key. Fastest replies, and the only option for anything other people use.

`claude_cli` uses your own Claude Pro/Max subscription, for personal use on your own machine. Install Claude Code, run `claude` once and log in, then set `provider: claude_cli`. The brain runs the official CLI in headless mode (`claude -p`) and connects your tools to it over MCP. It never touches the CLI's login token, which is what Anthropic bans for third-party apps. Replies start one to three seconds later than with the API because the CLI starts fresh each turn. If your CLI version rejects `--tools ""` or `--include-partial-messages`, update Claude Code or adjust `llm.cli_args`.

## Brain and satellites

On the machine that runs the brain (the Mac for now; any Linux box later):

```bash
python -m voiceagent serve --token <long-secret>
```

A Mac satellite on the same machine: `python -m voiceagent satellite --token <long-secret>`

### Android phone as a satellite (app)

The native app in `android/` is the recommended way. GitHub Actions builds the APK and puts it on the repo's Releases page, so installing it is a download on the phone. Setup is in [android/README.md](android/README.md).

### Android phone as a satellite (Termux)

The older route, still useful for a quick test without installing anything.

Install Termux and Termux:API from F-Droid (the Play Store versions are outdated). In Android settings, give Termux the microphone permission and turn off battery optimization for it. Then in Termux:

```bash
pkg update && pkg install python git pulseaudio termux-api
pip install websockets pyyaml
git clone <your repo> voiceagent && cd voiceagent
termux-wake-lock
pulseaudio --start --load="module-sles-source" --exit-idle-time=-1
python -m voiceagent satellite --server ws://<mac-lan-ip>:8765 --id living-room \
    --token <long-secret> --mic command --tts termux
```

Pair the phone to the Echo Dot over Bluetooth ("Alexa, pair") and replies play through it. To check the mic works before going further: `parec --raw --format=s16le --rate=16000 --channels=1 | head -c 64000 > /dev/null` should finish in about two seconds. If it hangs or the brain never hears the wake word, the mic permission is the usual cause.

## Single machine, step by step

```bash
python -m voiceagent chat              # talk to the brain by typing, no audio needed
python -m voiceagent govee-discover    # find lights (enable LAN Control in the Govee app first)
python -m voiceagent chat --speak      # typed input, spoken replies through the Echo
python -m voiceagent run               # all-in-one voice mode on the Mac: say "hey jarvis"
```

Grant the terminal microphone access when macOS asks. Keep the Mac awake with `caffeinate -ims` and the lid open (a closed MacBook disconnects its mic).

## Things to say

"Hey Jarvis, make the living room warm and dim." / "Set a timer for ten minutes for the pasta." / "Remember that Ceren likes the lights cool in the morning." / Or just talk. After each reply it keeps listening for six seconds, so you don't need the wake word again mid-conversation. Say "that's all" to end.

It answers in the language you speak. Turkish uses the Yelda voice; install it under System Settings, Accessibility, Spoken Content, System Voice, Manage Voices.

## Layout

```
voiceagent/
  server.py       the brain as a websocket service, one session per satellite
  satellite.py    thin client: streams mic audio, speaks replies, reconnects on its own
  assistant.py    all-in-one mode (same state machine, local mic)
  listening.py    end-of-speech detection as a streaming state machine
  audio.py        mic, wake word, Whisper STT
  tts.py          say, Piper, Android TTS via Termux, speaker queue
  agent.py        prompt building, streaming replies sentence by sentence, tool loop (API key)
  claude_cli.py   personal mode through the official claude CLI, plus a localhost tool relay
  mcp_bridge.py   tiny MCP server the CLI launches; forwards tool calls to the relay
  memory.py       SQLite: short-term conversation, long-term facts
  tools/          one file per capability (core.py, govee.py)
tests/            python -m unittest discover -s tests
```

Adding a capability is one function in `tools/` with a JSON schema. See ARCHITECTURE.md for where this is going.
