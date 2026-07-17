# Alexa to OpenClaw Bridge

Voice bridge between Amazon Echo devices and a self-hosted [OpenClaw](https://github.com/openclaw/openclaw) agent. MIT licensed.

Talk to your OpenClaw agent (running Claude on your subscription) through your Echo Dot Max.

```
Echo Dot Max -> Alexa cloud -> HTTPS (Cloudflare Tunnel) -> this bridge -> OpenClaw -> Claude
```

You say: "Alexa, ask my agent to check my calendar for tomorrow."
The bridge forwards the text to OpenClaw, waits for the reply, and Alexa speaks it.

## Prerequisites

- OpenClaw installed and working on your desktop PC. Test with:
  `openclaw agent --message "say hi"` in a terminal. If that answers, you are good.
- Claude subscription auth already set up (Claude Code login reused by OpenClaw, no API key).
- Node.js 18 or newer.
- If your desktop is Windows: run OpenClaw, this bridge, and the tunnel all inside the same WSL2 environment. Everything below is identical inside WSL.

## 1. Run the bridge

```bash
cd alexa-openclaw-bridge
npm install
npm start
```

You should see: `Alexa-OpenClaw bridge listening on http://localhost:3980/alexa`

Optional environment variables:

| Variable | Default | Purpose |
|---|---|---|
| PORT | 3980 | Bridge port |
| OPENCLAW_BIN | openclaw | Path to the CLI if not on PATH |
| OPENCLAW_AGENT_ID | (main) | Route to a specific agent |
| OPENCLAW_THINKING | low | Keep latency voice-friendly |
| AGENT_TIMEOUT_MS | 6500 | Alexa allows ~8s total, leave headroom |

## 2. Expose it with Cloudflare Tunnel

Alexa requires a public HTTPS endpoint with a trusted certificate. Cloudflare Tunnel provides that without opening ports on your router.

Quick test (temporary URL, changes on every restart):

```bash
# Install cloudflared first: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
cloudflared tunnel --url http://localhost:3980
```

It prints a URL like `https://random-words.trycloudflare.com`. Your skill endpoint is that URL plus `/alexa`.

For a permanent setup, create a named tunnel bound to a domain you own (free Cloudflare account):

```bash
cloudflared tunnel login
cloudflared tunnel create alexa-bridge
cloudflared tunnel route dns alexa-bridge alexa.yourdomain.com
cloudflared tunnel run --url http://localhost:3980 alexa-bridge
```

Endpoint: `https://alexa.yourdomain.com/alexa`

## 3. Create the Alexa Skill

1. Go to https://developer.amazon.com/alexa/console/ask and sign in with the SAME Amazon account your Echo Dot Max is registered to. This matters: skills in development mode are automatically available on your own devices only.
2. Create Skill.
   - Name: My Agent (anything you like)
   - Primary locale: match your Echo's language (e.g. English (US) or German (DE))
   - Type of experience: Other -> Custom
   - Hosting: "Provision your own"
   - Template: Start from Scratch
3. In the left menu open Interaction Model -> JSON Editor. Delete what is there, paste the contents of `interaction-model.json`, click Save, then Build skill.
   - If your Echo runs in German, change the sample utterances to German equivalents ("frage {query}", "nach {query}" etc.) and keep the structure.
4. Left menu -> Endpoint.
   - Select HTTPS.
   - Default Region: your tunnel URL including the path, e.g. `https://alexa.yourdomain.com/alexa`
   - SSL certificate type: "My development endpoint is a sub-domain of a domain that has a wildcard certificate from a certificate authority". (Cloudflare's cert qualifies.)
   - Save.
5. Build the model again if prompted.

## 4. Test

- In the console, open the Test tab, enable testing in Development, and type:
  `ask my agent to say hello`
- Then on the real device: "Alexa, ask my agent to say hello."
- Two-step also works: "Alexa, open my agent" -> "check my emails".

The skill stays in development mode forever, that is fine for personal use. No certification needed.

## Behavior notes

- Alexa hard-limits responses to about 8 seconds. If the agent needs longer, you will hear "The agent is still working on that" and OpenClaw will deliver the full answer to your configured chat channel (WhatsApp, Telegram, etc.).
- Replies are stripped of markdown and shortened to spoken-friendly length. The full text always exists in the OpenClaw session.
- The bridge verifies Amazon's request signature and timestamp, so random internet traffic to your tunnel URL cannot trigger your agent.
- The bridge and the tunnel must be running for the skill to work. Consider registering both as services (systemd inside WSL, or Task Scheduler launching WSL on boot).

## Troubleshooting

| Symptom | Fix |
|---|---|
| "There was a problem with the requested skill's response" | Bridge not running, tunnel down, or endpoint URL/path wrong in the console |
| "I could not reach the agent" | `openclaw gateway status` on the desktop, restart the gateway |
| Every answer is "still working" | Raise AGENT_TIMEOUT_MS slightly (max ~7000), lower thinking, or use a faster model for the voice agent |
| Signature verification errors in logs | Make sure no proxy modifies the request body; the tunnel must pass it through untouched |
