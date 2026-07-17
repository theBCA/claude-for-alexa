/**
 * Alexa <-> OpenClaw bridge server
 *
 * Flow: Echo Dot Max -> Alexa cloud -> HTTPS (Cloudflare Tunnel) -> this server
 *       -> `openclaw agent --message "<utterance>"` -> reply spoken back on the Echo.
 *
 * Alexa gives us roughly 8 seconds to respond. If the agent is slower, we
 * return a "still working" message and let OpenClaw deliver the full answer
 * to a chat channel (WhatsApp/Telegram/etc.) if you have one configured.
 *
 * Config via environment variables (all optional):
 *   PORT                 default 3980
 *   OPENCLAW_BIN         default "openclaw"
 *   OPENCLAW_AGENT_ID    if set, adds --agent <id>
 *   OPENCLAW_THINKING    default "low"  (keeps latency down for voice)
 *   AGENT_TIMEOUT_MS     default 6500   (leave headroom inside Alexa's 8s)
 *   BRIDGE_SHARED_PATH   default "/alexa" (URL path the skill points at)
 */

'use strict';

const express = require('express');
const { execFile } = require('child_process');
const Alexa = require('ask-sdk-core');
const { ExpressAdapter } = require('ask-sdk-express-adapter');

const PORT = parseInt(process.env.PORT || '3980', 10);
const OPENCLAW_BIN = process.env.OPENCLAW_BIN || 'openclaw';
const OPENCLAW_AGENT_ID = process.env.OPENCLAW_AGENT_ID || '';
const OPENCLAW_THINKING = process.env.OPENCLAW_THINKING || 'low';
const AGENT_TIMEOUT_MS = parseInt(process.env.AGENT_TIMEOUT_MS || '6500', 10);
const BRIDGE_PATH = process.env.BRIDGE_SHARED_PATH || '/alexa';

/* ------------------------------------------------------------------ */
/* OpenClaw invocation                                                 */
/* ------------------------------------------------------------------ */

/**
 * Sends a message to the OpenClaw agent via the CLI and resolves with the
 * agent's text reply. Rejects with { timedOut: true } when the agent does
 * not answer within AGENT_TIMEOUT_MS.
 */
function askOpenClaw(message) {
  return new Promise((resolve, reject) => {
    const args = ['agent', '--message', message, '--thinking', OPENCLAW_THINKING];
    if (OPENCLAW_AGENT_ID) {
      args.push('--agent', OPENCLAW_AGENT_ID);
    }

    const child = execFile(
      OPENCLAW_BIN,
      args,
      { timeout: AGENT_TIMEOUT_MS, maxBuffer: 1024 * 1024, killSignal: 'SIGKILL' },
      (error, stdout, stderr) => {
        if (error) {
          if (error.killed || error.signal === 'SIGKILL') {
            return reject({ timedOut: true });
          }
          console.error('[openclaw] error:', error.message, stderr && stderr.slice(0, 500));
          return reject({ timedOut: false, error });
        }
        resolve(cleanReply(stdout));
      }
    );

    child.on('error', (err) => reject({ timedOut: false, error: err }));
  });
}

/**
 * Strips markdown, code fences, and other things that sound terrible when
 * read aloud, and trims to a length Alexa can comfortably speak.
 */
function cleanReply(text) {
  let out = (text || '').trim();

  // Some CLI builds print JSON. If so, try to pull a text field.
  if (out.startsWith('{') || out.startsWith('[')) {
    try {
      const parsed = JSON.parse(out);
      out =
        parsed.text ||
        parsed.reply ||
        parsed.message ||
        (Array.isArray(parsed) && parsed.map((p) => p.text || '').join(' ')) ||
        out;
    } catch (_) {
      /* not JSON after all, keep raw */
    }
  }

  out = out
    .replace(/```[\s\S]*?```/g, ' code block omitted. ')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/\*([^*]+)\*/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/[<>&]/g, ' ') // avoid breaking SSML
    .replace(/\s+/g, ' ')
    .trim();

  // Alexa's outputSpeech limit is 8000 chars, but long monologues are
  // painful. Cut at a sentence boundary near 700 chars.
  const MAX = 700;
  if (out.length > MAX) {
    const cut = out.slice(0, MAX);
    const lastStop = Math.max(cut.lastIndexOf('. '), cut.lastIndexOf('! '), cut.lastIndexOf('? '));
    out = (lastStop > 200 ? cut.slice(0, lastStop + 1) : cut) + ' That is the short version. Check chat for the rest.';
  }

  return out || 'The agent replied with an empty message.';
}

/* ------------------------------------------------------------------ */
/* Alexa skill handlers                                                */
/* ------------------------------------------------------------------ */

const LaunchRequestHandler = {
  canHandle(handlerInput) {
    return Alexa.getRequestType(handlerInput.requestEnvelope) === 'LaunchRequest';
  },
  handle(handlerInput) {
    return handlerInput.responseBuilder
      .speak('Your agent is listening. What do you need?')
      .reprompt('What should the agent do?')
      .getResponse();
  },
};

const AskAgentIntentHandler = {
  canHandle(handlerInput) {
    return (
      Alexa.getRequestType(handlerInput.requestEnvelope) === 'IntentRequest' &&
      Alexa.getIntentName(handlerInput.requestEnvelope) === 'AskAgentIntent'
    );
  },
  async handle(handlerInput) {
    const query = Alexa.getSlotValue(handlerInput.requestEnvelope, 'query');

    if (!query) {
      return handlerInput.responseBuilder
        .speak("I didn't catch that. What should the agent do?")
        .reprompt('What should the agent do?')
        .getResponse();
    }

    console.log(`[alexa] utterance: ${query}`);

    try {
      const reply = await askOpenClaw(query);
      console.log(`[openclaw] reply: ${reply.slice(0, 200)}`);
      return handlerInput.responseBuilder
        .speak(reply)
        .reprompt('Anything else?')
        .getResponse();
    } catch (e) {
      if (e && e.timedOut) {
        return handlerInput.responseBuilder
          .speak('The agent is still working on that. It will send the answer to your chat when it finishes.')
          .getResponse();
      }
      console.error('[bridge] agent call failed:', e && e.error ? e.error.message : e);
      return handlerInput.responseBuilder
        .speak('I could not reach the agent. Make sure the OpenClaw gateway is running on your computer.')
        .getResponse();
    }
  },
};

const HelpIntentHandler = {
  canHandle(handlerInput) {
    return (
      Alexa.getRequestType(handlerInput.requestEnvelope) === 'IntentRequest' &&
      Alexa.getIntentName(handlerInput.requestEnvelope) === 'AMAZON.HelpIntent'
    );
  },
  handle(handlerInput) {
    return handlerInput.responseBuilder
      .speak('Say something like: ask my agent to check my calendar. What do you need?')
      .reprompt('What should the agent do?')
      .getResponse();
  },
};

const CancelAndStopIntentHandler = {
  canHandle(handlerInput) {
    return (
      Alexa.getRequestType(handlerInput.requestEnvelope) === 'IntentRequest' &&
      ['AMAZON.CancelIntent', 'AMAZON.StopIntent'].includes(
        Alexa.getIntentName(handlerInput.requestEnvelope)
      )
    );
  },
  handle(handlerInput) {
    return handlerInput.responseBuilder.speak('Okay.').getResponse();
  },
};

const FallbackIntentHandler = {
  canHandle(handlerInput) {
    return (
      Alexa.getRequestType(handlerInput.requestEnvelope) === 'IntentRequest' &&
      Alexa.getIntentName(handlerInput.requestEnvelope) === 'AMAZON.FallbackIntent'
    );
  },
  handle(handlerInput) {
    return handlerInput.responseBuilder
      .speak("I didn't get that. Try: ask my agent to, followed by your request.")
      .reprompt('What should the agent do?')
      .getResponse();
  },
};

const SessionEndedRequestHandler = {
  canHandle(handlerInput) {
    return Alexa.getRequestType(handlerInput.requestEnvelope) === 'SessionEndedRequest';
  },
  handle(handlerInput) {
    const reason =
      handlerInput.requestEnvelope.request && handlerInput.requestEnvelope.request.reason;
    console.log('[alexa] session ended:', reason);
    return handlerInput.responseBuilder.getResponse();
  },
};

const ErrorHandler = {
  canHandle() {
    return true;
  },
  handle(handlerInput, error) {
    console.error('[bridge] unhandled error:', error);
    return handlerInput.responseBuilder
      .speak('Something went wrong on the bridge. Check the server logs.')
      .getResponse();
  },
};

/* ------------------------------------------------------------------ */
/* Server                                                              */
/* ------------------------------------------------------------------ */

const skill = Alexa.SkillBuilders.custom()
  .addRequestHandlers(
    LaunchRequestHandler,
    AskAgentIntentHandler,
    HelpIntentHandler,
    CancelAndStopIntentHandler,
    FallbackIntentHandler,
    SessionEndedRequestHandler
  )
  .addErrorHandlers(ErrorHandler)
  .create();

// ExpressAdapter(skill, verifySignature, verifyTimestamp)
// Both verifications ON: rejects any request not genuinely signed by Amazon,
// which is required for self-hosted skill endpoints.
const adapter = new ExpressAdapter(skill, true, true);

const app = express();

app.get('/health', (_req, res) => res.json({ ok: true }));

// IMPORTANT: no express.json() before this route. The adapter needs the raw
// body to verify Amazon's request signature.
app.post(BRIDGE_PATH, adapter.getRequestHandlers());

app.listen(PORT, () => {
  console.log(`Alexa-OpenClaw bridge listening on http://localhost:${PORT}${BRIDGE_PATH}`);
  console.log(`Agent timeout: ${AGENT_TIMEOUT_MS}ms, thinking: ${OPENCLAW_THINKING}`);
});
