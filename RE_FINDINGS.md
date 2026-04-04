# ElevenLabs Reverse Engineering Findings
## Date: 2026-04-04
## API Account: bobbymcwilliams@echo-op.com (Creator tier, 131K chars)

---

## INFRASTRUCTURE
- **Backend**: uvicorn (Python/FastAPI confirmed from Server header)
- **Hosting**: GCP (34.8.184.191 = Google Cloud Platform, NOT Cloudflare)
- **Protocol**: HTTP/1.1 (no H2)
- **Request tracking**: X-Request-Id header, history-item-id for TTS
- **Concurrency tracking**: current-concurrent-requests, workspace-concurrent-requests headers

## TTS MODEL (eleven_v3)
- **Architecture**: AUTOREGRESSIVE (linear time vs text length confirmed)
- **TTFB**: ~700-800ms constant regardless of text length (model startup overhead)
- **Generation rate**: ~24-36 chars/sec after TTFB
- **Seed determinism**: NOT deterministic (3 identical requests, 3 different hashes)
  - Implies stochastic sampling, likely AR with temperature/top-p
- **Audio normalization**: Peak always hits 1.0 (hard normalization)
- **RMS energy**: ~0.573 across all emotion tags (loudness normalization)

## EMOTION TAGS (v3, voice=Roger, seed=42)
| Tag         | Duration(s) | Delta vs Neutral | Audio Bytes |
|-------------|-------------|------------------|-------------|
| neutral     | 1.99        | baseline         | 63,574      |
| excited     | 1.54        | -22.6%           | 49,364      |
| whispers    | 2.14        | +7.5%            | 68,590      |
| sad         | 1.93        | -3.0%            | 61,902      |
| angry       | 1.58        | -20.6%           | 50,618      |
| sarcastic   | 1.99        | +0.0%            | 63,574 (identical!) |
| sympathetic | 1.86        | -6.5%            | 59,394      |

Key: excited/angry = faster speech, whispers = slower, sarcastic = NO CHANGE

## AVAILABLE MODELS
| Model ID                  | Name                     | Languages |
|---------------------------|--------------------------|-----------|
| eleven_v3                 | Eleven v3                | 74        |
| eleven_multilingual_v2    | Eleven Multilingual v2   | 29        |
| eleven_flash_v2_5         | Eleven Flash v2.5        | 32        |
| eleven_turbo_v2_5         | Eleven Turbo v2.5        | 32        |
| eleven_turbo_v2           | Eleven Turbo v2          | 1         |
| eleven_flash_v2           | Eleven Flash v2          | 1         |
| eleven_english_sts_v2     | Eleven English v2        | 1         |
| eleven_monolingual_v1     | Eleven English v1        | 1         |
| eleven_multilingual_sts_v2| Eleven Multilingual v2   | 29        |
| eleven_multilingual_v1    | Eleven Multilingual v1   | 9         |

## MULTI-SPEAKER DIALOGUE
- Endpoint: POST /v1/text-to-dialogue/stream
- TTFB: ~900ms, Total: 4.27s for 3 speakers, 138KB audio
- Works with v3 model

## CONVAI AGENT
- Created agent: agent_3501kncah0rwfnnrapev3t0ad8y5
- Turn mode options: "silence" or "turn" (NOT "smooth" as some docs say)
- English agents MUST use turbo or flash v2 (v3 not allowed for ConvAI English)
- ConvAI conversation ID format: conv_XXXX (custom prefix, not UUID)
- Event IDs: sequential integers
- Ping keepalive: ~1.8s (very aggressive, not 10s)
- First message auto-sent as agent_response immediately after init

## CONVAI WEBSOCKET PROTOCOL (LIVE CAPTURE)
Connection: wss://api.elevenlabs.io/v1/convai/conversation?agent_id=XXXX

Messages observed:
1. Client sends: conversation_initiation_client_data
2. Server sends: conversation_initiation_metadata (conversation_id, formats)
3. Server sends: audio (first message TTS audio, base64 PCM)
4. Server sends: agent_response (first message text, event_id=1)
5. Server sends: ping (event_id=2, ping_ms=null) at ~1.8s
6. Client sends: pong (event_id=2)

## SDK ANALYSIS HIGHLIGHTS (from Python + JS SDK extraction)
- 3 separate WebSocket protocols: ConvAI, Realtime TTS, Realtime STT/Scribe
- WebRTC via LiveKit is NOW DEFAULT for voice (WebSocket is legacy)
- LiveKit server: wss://livekit.rtc.elevenlabs.io
- Default LLM: gemini-2.5-flash
- 50+ LLM options including GPT-5.x, Gemini 3.x, Claude Sonnet 4.6
- Hidden coaching system (coached/coaching agent pairs)
- PII redaction with 50+ entity types
- Batch calling up to 10K recipients
- Workflow graph builder with expression-based edge conditions
