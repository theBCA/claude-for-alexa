"""CLI entry point.

  python -m voiceagent serve           the brain: Claude, memory, tools, speech recognition
  python -m voiceagent satellite       a mic + speaker that connects to the brain (Mac, Android, Pi)
  python -m voiceagent run             all-in-one voice mode on one machine, no network
  python -m voiceagent chat            text mode for testing the brain and tools
  python -m voiceagent govee-discover  find Govee lights with LAN control on
  python -m voiceagent roborock-login  link your Roborock account once (email code)
  python -m voiceagent audio-devices   list mics and speakers
"""
from __future__ import annotations

import argparse
import logging

from .config import load_config


def main() -> None:
    p = argparse.ArgumentParser(prog="voiceagent")
    p.add_argument("command", choices=["serve", "satellite", "run", "chat", "govee-discover", "roborock-login",
                                        "audio-devices"])
    p.add_argument("-c", "--config", help="path to config.yaml")
    p.add_argument("--speak", action="store_true", help="chat mode: also speak replies")
    p.add_argument("-v", "--verbose", action="store_true")
    sat = p.add_argument_group("satellite options (override config.yaml)")
    sat.add_argument("--server", help="brain address, e.g. ws://192.168.1.20:8765")
    sat.add_argument("--id", help="name of this satellite, e.g. living-room")
    sat.add_argument("--token", help="shared secret (also used by serve)")
    sat.add_argument("--mic", choices=["sounddevice", "command"])
    sat.add_argument("--mic-command", help="command that writes raw 16 kHz mono s16le PCM to stdout")
    sat.add_argument("--tts", choices=["say", "piper", "termux", "print"])
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S",
    )
    cfg = load_config(args.config)
    overrides = {"server": args.server, "id": args.id, "token": args.token, "mic": args.mic,
                 "mic_command": args.mic_command, "tts_engine": args.tts}
    cfg["satellite"].update({k: v for k, v in overrides.items() if v is not None})
    if args.token:
        cfg["server"]["token"] = args.token

    if args.command == "serve":
        import asyncio
        from .server import Brain
        try:
            asyncio.run(Brain(cfg).serve())
        except KeyboardInterrupt:
            print("\nbye")
        return
    if args.command == "satellite":
        import asyncio
        from .satellite import run_satellite
        try:
            asyncio.run(run_satellite(cfg))
        except KeyboardInterrupt:
            print("\nbye")
        return

    if args.command == "audio-devices":
        import sounddevice as sd
        print(sd.query_devices())
    elif args.command == "govee-discover":
        from .tools.govee import discover
        found = discover(timeout=3)
        if not found:
            print("No lights answered. Enable 'LAN Control' in the Govee Home app and check the same Wi-Fi.")
        for d in found:
            print(f"{d['ip']:16} {d['sku']:10} {d['device']}")
    elif args.command == "roborock-login":
        from .tools.roborock import CREDENTIALS, login
        login(input("Roborock account email: ").strip())
        print(f"Saved to {CREDENTIALS}. Restart the brain and ask Jarvis about the vacuum.")
    elif args.command == "chat":
        chat(cfg, args.speak)
    else:
        from .assistant import Assistant
        try:
            Assistant(cfg).run()
        except KeyboardInterrupt:
            print("\nbye")


def chat(cfg, speak: bool) -> None:
    from .agent import make_agent
    from .assistant import build_tools
    from .memory import Memory

    memory = Memory(cfg.memory.path)
    tools = build_tools(cfg, memory)
    agent = make_agent(cfg, tools, memory)
    speaker = None
    if speak:
        from .tts import Speaker, make_tts
        speaker = Speaker(make_tts(cfg.tts))
        tools.ctx["announce"] = lambda t: speaker.say(t)
    else:
        tools.ctx["announce"] = lambda t: print(f"\n[{cfg.assistant.name}] {t}")

    print(f"Text chat with {cfg.assistant.name}. Empty line to quit.")
    while True:
        try:
            text = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            break

        def on_sentence(s: str) -> None:
            print(s, end=" ", flush=True)
            if speaker:
                speaker.say(s)

        print(f"{cfg.assistant.name.lower()}> ", end="", flush=True)
        agent.respond(text, on_sentence)
        print()
        if speaker:
            speaker.wait()


if __name__ == "__main__":
    main()
