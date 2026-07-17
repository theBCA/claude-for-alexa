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

## 3. Register the Alexa Skill

Sign in to https://developer.amazon.com/alexa/console/ask with the SAME Amazon account your Echo Dot Max is registered to first — this matters: skills in development mode are automatically available on your own devices only, no publishing step involved.

### Option A: `ask-cli` (recommended — one command, repeatable)

The `skill-package/` directory in this repo is a ready-to-deploy ASK CLI project.

```bash
npm install -g ask-cli
ask configure              # links your Amazon developer account (skip the AWS/Lambda prompts — this skill is self-hosted)
```

Before the first deploy, edit `skill-package/skill.json` and replace
`https://REPLACE-WITH-YOUR-TUNNEL-URL/alexa` with your actual tunnel URL + `/alexa`
from step 2.

```bash
ask deploy
```

This creates the skill on your account and uploads both the manifest and the
interaction model in one shot. Your account-specific skill ID gets written to
`.ask/` (gitignored — it's per-developer state, not something to commit).

Whenever you change `skill-package/skill.json` or
`skill-package/interactionModels/custom/en-US.json` (or your tunnel URL
changes), just run `ask deploy` again.

> Do not run `ask smapi submit-skill-for-certification-request` — that starts
> Amazon's public store review process, which this personal setup isn't meant
> for (see "Going public later" below).

### Option B: Manual console setup

1. Create Skill.
   - Name: My Agent (anything you like)
   - Primary locale: match your Echo's language (e.g. English (US) or German (DE))
   - Type of experience: Other -> Custom
   - Hosting: "Provision your own"
   - Template: Start from Scratch
2. In the left menu open Interaction Model -> JSON Editor. Delete what is there, paste the contents of `skill-package/interactionModels/custom/en-US.json`, click Save, then Build skill.
   - If your Echo runs in German, change the sample utterances to German equivalents ("frage {query}", "nach {query}" etc.) and keep the structure.
3. Left menu -> Endpoint.
   - Select HTTPS.
   - Default Region: your tunnel URL including the path, e.g. `https://alexa.yourdomain.com/alexa`
   - SSL certificate type: "My development endpoint is a sub-domain of a domain that has a wildcard certificate from a certificate authority". (Cloudflare's cert qualifies.)
   - Save.
4. Build the model again if prompted.

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

## Going public later

This skill is deliberately built for single-user, development-mode use — the
endpoint is your own tunnel, and every request goes to your own OpenClaw
agent. Opening it up to other users later would need real changes, not just
flipping a flag:

- A stable multi-tenant endpoint instead of a personal Cloudflare tunnel (e.g.
  each user's skill instance pointing at their own bridge, or a hosted
  service that routes by account).
- Per-user auth/account linking, so one person's voice requests can't reach
  another person's agent.
- A privacy policy URL and completed `privacyAndCompliance` fields in
  `skill-package/skill.json`.
- Passing Amazon's certification review (`ask smapi submit-skill-for-certification-request`
  or the console's Distribution tab).

None of that is set up yet, and isn't needed for personal use — just keeping
it in mind for when/if this becomes a shared project.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "There was a problem with the requested skill's response" | Bridge not running, tunnel down, or endpoint URL/path wrong in the console |
| "I could not reach the agent" | `openclaw gateway status` on the desktop, restart the gateway |
| Every answer is "still working" | Raise AGENT_TIMEOUT_MS slightly (max ~7000), lower thinking, or use a faster model for the voice agent |
| Signature verification errors in logs | Make sure no proxy modifies the request body; the tunnel must pass it through untouched |
