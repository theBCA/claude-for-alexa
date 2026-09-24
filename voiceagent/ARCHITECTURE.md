# Architecture and product direction

## What separates a product from a weekend script

A demo that works when you speak clearly at the laptop is easy. What makes people keep using a voice assistant is how it behaves in the messy cases, so v0.1 is built around those.

**Latency.** Users judge the whole thing by the gap between finishing a sentence and hearing a reply. The budget in v0.1: about 0.8 s of silence to detect you've stopped, 0.3 to 1 s for Whisper, then time to the first sentence from Claude. Replies stream and each sentence is spoken as soon as it's complete, so a long answer starts after the first sentence rather than after the whole paragraph. The log prints "first audio after X s" on every turn; that number is the main metric to drive down. Haiku is the default model for this reason.

**Hearing itself.** A Bluetooth speaker plays audio a few hundred ms late, and the Mac mic will pick it up. v0.1 mutes the mic while speaking plus a short tail. That's reliable but blocks interrupting it mid-sentence (barge-in), which is the top item for v0.2 and needs real echo cancellation.

**Conversation, not commands.** Follow-up mode keeps the mic open after each reply. Short-term history expires after a few idle hours so each day starts clean, while long-term facts persist until you ask it to forget. The model decides what's worth remembering through the `remember` tool, and every fact is visible and deletable.

**Speech recognition errors.** Whisper hallucinates phrases on silence ("thank you", subtitle credits in Turkish). These are filtered, and the prompt tells the model to ask a short question rather than act on an unclear transcript when the action matters.

**Multilingual by default.** Whisper detects the language per utterance, the model replies in that language, and TTS picks a matching voice. Switching between Turkish, English and German mid-conversation just works, which almost no commercial assistant does well.

**Local where it matters.** Wake word and speech recognition run on-device, so audio never leaves the machine; only the transcribed text goes to the LLM. Govee lights are controlled over LAN, so they respond in tens of milliseconds and work if the cloud is down.

## The one architectural decision that matters most

Split the **satellite** (mic and speaker in a room) from the **brain** (wake word, speech recognition, agent, memory, tools). This is done as of v0.2: the brain is a websocket server, and satellites stream audio in and receive sentences to speak. That unlocks an Android phone as a device, one per room, and a brain that can move to a home server. Every product in this space that scaled (Sonos, Home Assistant Voice, Echo itself) ends up with this shape.

The protocol is documented at the top of `server.py`. Satellites speak using their own TTS, so the brain sends text, not audio. That keeps bandwidth tiny and lets each device use the best local voice it has.

Wake word currently runs on the brain, so satellites stream audio continuously (about 32 KB/s each). On a home network that's fine. It's the wrong choice for a cloud brain or battery-powered devices, and the fix is moving wake word onto the satellite in a native app, with no protocol change: the satellite simply starts streaming after it hears the wake word.

Where the brain lives matters for tools. Govee LAN control needs the brain on the home network. A cloud brain would need either Govee's cloud API or satellites that run local tools on its behalf. The second is the better product design and is on the roadmap.

## Model providers

Providers sit behind one interface (`respond(text, on_sentence, lang)`), so the rest of the system doesn't care who answers. `anthropic` uses an API key. `claude_cli` drives the official Claude Code CLI headlessly with the user's own subscription and exposes the tool registry to it through a stdio MCP server that relays calls back into the brain, so timers and memory behave the same in both modes. Subscription mode is strictly personal: Anthropic and Google both prohibit third-party apps from routing other users' requests through consumer subscription logins. A product for other people uses API keys on the server, or a user-pays service like OpenRouter's OAuth.

## Roadmap

v0.2, make it pleasant to live with: barge-in with echo cancellation, streaming TTS (Piper or a cloud voice like ElevenLabs) so speech starts mid-sentence, a fast local path for common commands like "lights off" that skips the LLM entirely, prompt caching to cut cost and latency, a Gemini provider behind the same interface, and a Home Assistant tool so Lepro and anything else HA supports becomes reachable.

v0.3, make it a platform: a native Android satellite app with on-device wake word (so the phone only streams after it hears you), MCP client support so any MCP server becomes a tool (calendar, email, Spotify), and a small web dashboard to see memory, conversation history and latency stats.

v1.0, make it something others can install: one-command installer, onboarding that finds lights and speakers automatically, household profiles with speaker identification, and confirmation rules for risky actions (sending messages, purchases).

## Open product questions

Who is the first customer: Home Assistant tinkerers who want a smarter voice, or ordinary people who want "Alexa but actually smart"? The first group will install Python; the second needs hardware or an app. Bring-your-own API key versus a subscription that bundles the model cost also changes a lot. Worth deciding before v0.3, since it shapes the satellite choice.
