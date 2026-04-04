# ElevenLabs OpenAPI Spec Deep Analysis

**Source:** `C:\ECHO_OMEGA_PRIME\elevenlabs_openapi_spec.json` (1.4MB)  
**Version:** 1.0 | **Total Paths:** 220 | **Total Schemas:** 1,039  
**Analyzed:** 2026-04-04  
**Servers:** None defined in spec (base URL is `https://api.elevenlabs.io`)

---

## Table of Contents

1. [Infrastructure & Security](#1-infrastructure--security)
2. [ConvAI Endpoints (92 endpoints)](#2-convai-endpoints)
3. [TTS Endpoints](#3-tts-endpoints)
4. [Voice Endpoints](#4-voice-endpoints)
5. [Agent Configuration Schemas](#5-agent-configuration-schemas)
6. [Workflow/Graph Builder](#6-workflowgraph-builder)
7. [Widget Configuration](#7-widget-configuration)
8. [WebSocket / Client Events](#8-websocket--client-events)
9. [MCP Server Integration](#9-mcp-server-integration)
10. [Telephony & Batch Calling](#10-telephony--batch-calling)
11. [Knowledge Base](#11-knowledge-base)
12. [Testing & Simulation](#12-testing--simulation)
13. [Environment Variables & Secrets](#13-environment-variables--secrets)
14. [Hidden Gems & Intelligence](#14-hidden-gems--intelligence)

---

## 1. Infrastructure & Security

### Authentication
- **Header:** `xi-api-key` (optional on all endpoints, passed as header parameter)
- **No OAuth flows defined** in the spec
- No security schemes at the top level - auth is per-endpoint via `xi-api-key` header param
- Some endpoints accept no auth at all (public LLM cost calculator)

### Server URLs
- **None specified in the spec** - consumers must know `https://api.elevenlabs.io`
- LiveKit tokens for real-time: obtained via `GET /v1/convai/conversation/token`
- Signed WebSocket URLs: obtained via `GET /v1/convai/conversation/get-signed-url`

### Rate Limits
- **Not defined in the OpenAPI spec** - must be obtained from docs or response headers
- Batch calling has `target_concurrency_limit` (min: 1) per batch
- Agent has `agent_concurrency_limit` (default: -1 = unlimited) and `daily_limit` (default: 100,000)
- Bursting: allowed up to 3x workspace limit at double rate when `bursting_enabled: true`
- MCP servers limited to **max 10 per agent** (`maxItems: 10` on `mcp_server_ids`)

---

## 2. ConvAI Endpoints

**92 total endpoints** under `/v1/convai/`

### 2.1 Agent CRUD (Core)

| Method | Path | Operation | Notes |
|--------|------|-----------|-------|
| GET | `/v1/convai/agents` | `get_agents_route` | Paginated, search, filter by archived/owner/sort |
| POST | `/v1/convai/agents/create` | `create_agent_route` | `conversation_config` required. Optional `enable_versioning` |
| GET | `/v1/convai/agents/{agent_id}` | `get_agent_route` | Supports `version_id` and `branch_id` query params |
| PATCH | `/v1/convai/agents/{agent_id}` | `patch_agent_settings_route` | Partial update, supports `enable_versioning_if_not_enabled` |
| DELETE | `/v1/convai/agents/{agent_id}` | `delete_agent_route` | |
| POST | `/v1/convai/agents/{agent_id}/avatar` | `post_agent_avatar_route` | File upload |
| POST | `/v1/convai/agents/{agent_id}/duplicate` | `duplicate_agent_route` | Clone an agent |
| GET | `/v1/convai/agents/{agent_id}/link` | `get_agent_link_route` | Get shareable link |
| GET | `/v1/convai/agents/summaries` | `get_agent_summaries_route` | Bulk summary by IDs |

**Create Agent Body:**
```json
{
  "conversation_config": { // REQUIRED - ConversationalConfigAPIModel
    "asr": { /* ASR config */ },
    "turn": { /* turn detection */ },
    "tts": { /* TTS config */ },
    "conversation": { /* conversation events/limits */ },
    "language_presets": { /* per-language overrides */ },
    "vad": { /* voice activity detection */ },
    "agent": { /* prompt, LLM, tools, first_message */ }
  },
  "platform_settings": { /* widget, evaluation, data_collection, guardrails */ },
  "workflow": { /* graph nodes + edges */ },
  "name": "My agent",
  "tags": ["Customer Support", "Sales"]
}
```

### 2.2 Agent Branching & Versioning

| Method | Path | Operation | Notes |
|--------|------|-----------|-------|
| POST | `/agents/{id}/branches` | `create_branch_route` | Create new branch |
| GET | `/agents/{id}/branches` | `get_branches_route` | List branches, `include_archived` |
| GET | `/agents/{id}/branches/{branch_id}` | `get_branch_route` | |
| PATCH | `/agents/{id}/branches/{branch_id}` | `update_branch_route` | |
| POST | `/agents/{id}/branches/{src}/merge` | `merge_branch_into_target` | Requires `target_branch_id` query param |
| POST | `/agents/{id}/drafts` | `create_agent_draft_route` | Requires `branch_id` |
| DELETE | `/agents/{id}/drafts` | `delete_agent_draft_route` | |
| POST | `/agents/{id}/deployments` | `create_agent_deployment_route` | Traffic splitting |

**Branch Protection:** `writer_perms_required` | `admin_perms_required`

**Deployment Strategy (Traffic Splitting):**
```json
{
  "requests": [{
    "branch_id": "agtbrch_...",
    "deployment_strategy": {
      "type": "percentage",
      "traffic_percentage": 50.0  // 0-100
    }
  }]
}
```

### 2.3 Conversations & History

| Method | Path | Operation | Notes |
|--------|------|-----------|-------|
| GET | `/conversations` | `get_conversation_histories_route` | 20+ filter params |
| GET | `/conversations/{id}` | `get_conversation_history_route` | Full transcript + analysis |
| DELETE | `/conversations/{id}` | `delete_conversation_route` | |
| GET | `/conversations/{id}/audio` | `get_conversation_audio_route` | Download audio |
| POST | `/conversations/{id}/feedback` | `post_conversation_feedback_route` | No auth required |
| POST | `/conversations/{id}/analysis/run` | `run_conversation_analysis` | Re-run analysis |
| POST | `/conversations/{id}/files` | `upload_file_route` | In-conversation file upload |
| DELETE | `/conversations/{id}/files/{file_id}` | `cancel_file_upload_route` | |

**Conversation List Filters:**
- `call_successful`, `call_start_before_unix`, `call_start_after_unix`
- `call_duration_min_secs`, `call_duration_max_secs`
- `rating_max`, `rating_min`, `has_feedback_comment`
- `user_id`, `evaluation_params`, `data_collection_params`
- `tool_names`, `tool_names_successful`, `tool_names_errored`
- `main_languages`, `conversation_initiation_source`, `branch_id`
- `summary_mode`, `search`

**Message Search:**
- `GET /conversations/messages/smart-search` - Semantic/AI search
- `GET /conversations/messages/text-search` - Exact text search with all conversation filters

### 2.4 Connection Endpoints

| Method | Path | Operation | Notes |
|--------|------|-----------|-------|
| GET | `/conversation/get-signed-url` | `get_conversation_signed_link` | WebSocket signed URL |
| GET | `/conversation/get_signed_url` | `get_signed_url_deprecated` | **DEPRECATED** duplicate |
| GET | `/conversation/token` | `get_livekit_token` | LiveKit room token |

**Signed URL params:** `agent_id` (required), `include_conversation_id`, `branch_id`, `environment`  
**Token params:** `agent_id` (required), `participant_name`, `branch_id`, `environment`

### 2.5 Analytics

| Method | Path | Operation |
|--------|------|-----------|
| GET | `/analytics/live-count` | `get_live_count` |

Response: `{ "count": 42 }` - number of active ongoing conversations

### 2.6 Settings

| Method | Path | Operation |
|--------|------|-----------|
| GET | `/settings` | `get_convai_settings` |
| PATCH | `/settings` | `patch_convai_settings` |
| GET | `/settings/dashboard` | `get_dashboard_settings` |
| PATCH | `/settings/dashboard` | `patch_dashboard_settings` |

### 2.7 Users

| Method | Path | Operation |
|--------|------|-----------|
| GET | `/users` | `get_convai_users` |

### 2.8 LLM Management

| Method | Path | Operation | Notes |
|--------|------|-----------|-------|
| GET | `/llm/list` | `list_available_llms` | All available LLMs |
| POST | `/llm-usage/calculate` | `get_public_llm_expected_cost_calculation` | Cost calculator (no auth) |
| POST | `/agent/{id}/llm-usage/calculate` | `get_agent_llm_expected_cost_calculation` | Per-agent cost |

---

## 3. TTS Endpoints

### 3.1 Text-to-Speech

| Method | Path | Operation | Response |
|--------|------|-----------|----------|
| POST | `/v1/text-to-speech/{voice_id}` | `text_to_speech_full` | `audio/mpeg` |
| POST | `/v1/text-to-speech/{voice_id}/stream` | `text_to_speech_stream` | `audio/mpeg` stream |
| POST | `/v1/text-to-speech/{voice_id}/with-timestamps` | `text_to_speech_full_with_timestamps` | JSON with timestamps |
| POST | `/v1/text-to-speech/{voice_id}/stream/with-timestamps` | `text_to_speech_stream_with_timestamps` | JSON stream |

**TTS Request Body (full schema):**
```
text: string (required) - The text to convert
model_id: string (default: "eleven_multilingual_v2") 
language_code: string|null - ISO 639-1 language code
voice_settings: VoiceSettingsResponseModel|null - Per-request overrides
pronunciation_dictionary_locators: array|null - Up to 3 dictionaries
seed: integer|null - 0..4294967295 for deterministic output
previous_text: string|null - Context for continuation
next_text: string|null - Lookahead context
previous_request_ids: string[]|null - For multi-request sessions
next_request_ids: string[]|null
apply_text_normalization: enum("auto"|"on"|"off"|"apply_english") - default "auto"
```

**Query Parameters on TTS endpoints:**
- `output_format` - Audio output format
- `optimize_streaming_latency` - 0-4 optimization level (deprecated on some)
- `enable_logging` - default true
- `enable_ssml_parsing` - default false

### 3.2 Text-to-Dialogue (Multi-Voice)

| Method | Path | Operation | Response |
|--------|------|-----------|----------|
| POST | `/v1/text-to-dialogue` | `text_to_dialogue` | `audio/mpeg` |
| POST | `/v1/text-to-dialogue/stream` | `text_to_dialogue_stream` | `audio/mpeg` stream |
| POST | `/v1/text-to-dialogue/with-timestamps` | `text_to_dialogue_full_with_timestamps` | JSON |
| POST | `/v1/text-to-dialogue/stream/with-timestamps` | `text_to_dialogue_stream_with_timestamps` | JSON stream |

**Dialogue Request Body:**
```
inputs: DialogueInput[] (required) - Max 10 unique voice IDs
  - text: string (required)
  - voice_id: string (required)
model_id: string (default: "eleven_v3")
language_code: string|null
settings: ModelSettingsResponseModel|null
  - stability: number (0.0-1.0, default 0.5)
pronunciation_dictionary_locators: array|null
seed: integer|null
```

### 3.3 Speech-to-Speech

| Method | Path | Operation |
|--------|------|-----------|
| POST | `/v1/speech-to-speech/{voice_id}` | `speech_to_speech_full` |
| POST | `/v1/speech-to-speech/{voice_id}/stream` | `speech_to_speech_stream` |

### 3.4 Speech-to-Text (Scribe)

| Method | Path | Operation | Notes |
|--------|------|-----------|-------|
| POST | `/v1/speech-to-text` | `speech_to_text` | Returns 200 or 202 (async) |
| GET | `/v1/speech-to-text/transcripts/{id}` | `get_transcript_by_id` | |
| DELETE | `/v1/speech-to-text/transcripts/{id}` | `delete_transcript_by_id` | |

**STT Models:** `scribe_v1`, `scribe_v2`

**STT Request Body:**
```
model_id: enum("scribe_v1"|"scribe_v2") (required)
file: binary|null - Max 3.0GB, min 100ms audio
cloud_storage_url: string|null - Alternative to file upload
language_code: string|null - ISO-639-1 or ISO-639-3
tag_audio_events: boolean (default true) - Tag (laughter), (footsteps), etc.
num_speakers: integer|null (1-32) - Speaker count hint
timestamps_granularity: enum("none"|"word"|"character") (default "word")
diarize: boolean (default false) - Speaker annotation
diarization_threshold: number|null
additional_formats: string[]|null
```

### 3.5 TTS Model IDs

**Standard TTS models (for REST API):**
- `eleven_multilingual_v2` (default for TTS)
- `eleven_v3` (default for dialogue)

**Conversational TTS models (for ConvAI agents):**
- `eleven_flash_v2` (default, low latency)
- `eleven_flash_v2_5`
- `eleven_multilingual_v2`
- `eleven_v3_conversational`
- `eleven_turbo_v2` (DEPRECATED -> use flash_v2)
- `eleven_turbo_v2_5` (DEPRECATED -> use flash_v2_5)

### 3.6 Voice Settings

```
stability: number (0.0-1.0, default 0.5)
  Lower = broader emotional range, Higher = monotonous
similarity_boost: number (0.0-1.0, default 0.75)
  How closely to adhere to original voice
use_speaker_boost: boolean (default true)
  Boosts similarity, increases latency
style: number (default 0.0)
  Style exaggeration, increases latency if >0
speed: number (0.7-1.2, default 1.0)
  Playback speed
```

### 3.7 Audio Output Formats (ConvAI)

```
pcm_8000, pcm_16000 (default), pcm_22050, pcm_24000, pcm_44100, pcm_48000, ulaw_8000
```

---

## 4. Voice Endpoints

### 4.1 Voice CRUD

| Method | Path | Operation | Notes |
|--------|------|-----------|-------|
| GET | `/v1/voices` | `get_voices` | List all. `show_legacy` param |
| GET | `/v2/voices` | `get_user_voices_v2` | V2 with pagination, search, sort, filter |
| GET | `/v1/voices/{voice_id}` | `get_voice_by_id` | `with_settings` param |
| DELETE | `/v1/voices/{voice_id}` | `delete_voice` | |
| POST | `/v1/voices/{voice_id}/edit` | `edit_voice` | |
| POST | `/v1/voices/add` | `add_voice` | IVC (Instant Voice Clone) |
| POST | `/v1/voices/add/{public_user_id}/{voice_id}` | `add_sharing_voice` | Add shared voice |

### 4.2 Voice Settings

| Method | Path | Operation |
|--------|------|-----------|
| GET | `/v1/voices/settings/default` | `get_voice_settings_default` |
| GET | `/v1/voices/{voice_id}/settings` | `get_voice_settings` |
| POST | `/v1/voices/{voice_id}/settings/edit` | `edit_voice_settings` |

### 4.3 PVC (Professional Voice Clone)

| Method | Path | Operation |
|--------|------|-----------|
| POST | `/v1/voices/pvc` | `create_pvc_voice` |
| POST | `/v1/voices/pvc/{voice_id}` | `edit_pvc_voice` |
| GET | `/v1/voices/pvc/{voice_id}/captcha` | `get_pvc_voice_captcha` |
| POST | `/v1/voices/pvc/{voice_id}/captcha` | `verify_pvc_voice_captcha` |
| POST | `/v1/voices/pvc/{voice_id}/samples` | `add_pvc_voice_samples` |
| POST | `/v1/voices/pvc/{voice_id}/samples/{id}` | `edit_pvc_voice_sample` |
| DELETE | `/v1/voices/pvc/{voice_id}/samples/{id}` | `delete_pvc_voice_sample` |
| GET | `/v1/voices/pvc/{voice_id}/samples/{id}/audio` | `get_pvc_sample_audio` |
| POST | `/v1/voices/pvc/{voice_id}/samples/{id}/separate-speakers` | `start_speaker_separation` |
| GET | `/v1/voices/pvc/{voice_id}/samples/{id}/speakers` | `get_pvc_sample_speakers` |
| GET | `/v1/voices/pvc/{voice_id}/samples/{id}/speakers/{sid}/audio` | `get_speaker_audio` |
| GET | `/v1/voices/pvc/{voice_id}/samples/{id}/waveform` | `get_pvc_sample_visual_waveform` |
| POST | `/v1/voices/pvc/{voice_id}/train` | `run_pvc_voice_training` |
| POST | `/v1/voices/pvc/{voice_id}/verification` | `request_pvc_manual_verification` |

### 4.4 Voice Response Model

```
voice_id: string
name: string
category: enum("generated"|"cloned"|"premade"|"professional"|"famous"|"high_quality")
samples: SampleResponseModel[]|null
fine_tuning: FineTuningResponseModel|null
labels: { accent, age, description, gender, use_case }
description: string|null
preview_url: string|null
available_for_tiers: string[]
high_quality_base_model_ids: string[]
verified_languages: VerifiedVoiceLanguageResponseModel[]|null
settings: VoiceSettingsResponseModel|null
sharing: VoiceSharingResponseModel|null
is_legacy: boolean
is_mixed: boolean
is_owner: boolean
```

### 4.5 V2 Voices Query Params

```
next_page_token, page_size, search, sort, sort_direction,
voice_type, category, fine_tuning_state, collection_id,
include_total_count, voice_ids
```

### 4.6 Other Voice Endpoints

| Method | Path | Operation |
|--------|------|-----------|
| GET | `/v1/shared-voices` | List shared voices from library |
| POST | `/v1/similar-voices` | Find similar voices |
| POST | `/v1/text-to-voice` | Generate voice from text description |
| POST | `/v1/text-to-voice/create-previews` | Preview generated voices |
| POST | `/v1/text-to-voice/design` | Design voice from parameters |
| POST | `/v1/text-to-voice/{id}/remix` | Remix a generated voice |
| GET | `/v1/text-to-voice/{id}/stream` | Stream generated voice audio |

---

## 5. Agent Configuration Schemas

### 5.1 Top-Level Agent Config (`ConversationalConfigAPIModel`)

```
asr:          ASRConversationalConfig       - Speech recognition
turn:         TurnConfig                    - Turn detection
tts:          TTSConversationalConfig       - Text-to-speech  
conversation: ConversationConfig            - Events, limits
language_presets: { lang_code: LanguagePreset }
vad:          VADConfig                     - Voice activity detection
agent:        AgentConfigAPIModel           - Prompt, LLM, tools
```

### 5.2 Agent Config (`AgentConfigAPIModel`)

```
first_message: string (default "") - Agent's opening line
language: string (default "en") - ISO language code
hinglish_mode: boolean (default false) - Hindi+English mode
dynamic_variables: DynamicVariablesConfig
disable_first_message_interruptions: boolean (default false)
max_conversation_duration_message: string (default "")
prompt: PromptAgentAPIModel
```

### 5.3 Prompt Config (`PromptAgentAPIModel`)

```
prompt: string (default "") - System prompt
llm: LLM enum (default "gemini-2.5-flash")
reasoning_effort: LLMReasoningEffort|null (none|minimal|low|medium|high)
thinking_budget: integer|null - Max thinking tokens (0 = off)
temperature: number (default 0.0)
max_tokens: integer (default -1, 0 = unlimited)
tool_ids: string[] - Custom tool IDs
built_in_tools: BuiltInTools
mcp_server_ids: string[] (max 10)
knowledge_base: KnowledgeBaseLocator[]
custom_llm_extra_body: object
```

### 5.4 Available LLMs (Full Enum)

**OpenAI:**
- gpt-3.5-turbo, gpt-4, gpt-4-turbo, gpt-4o, gpt-4o-mini
- gpt-4.1, gpt-4.1-mini, gpt-4.1-nano
- gpt-5, gpt-5.1, gpt-5.2, gpt-5.2-chat-latest, gpt-5-mini, gpt-5-nano
- gpt-oss-20b, gpt-oss-120b
- Dated: gpt-5-2025-08-07, etc.

**Google:**
- gemini-1.5-pro, gemini-1.5-flash
- gemini-2.0-flash, gemini-2.0-flash-lite
- gemini-2.5-flash (DEFAULT), gemini-2.5-flash-lite
- gemini-3-pro-preview, gemini-3-flash-preview, gemini-3.1-flash-lite-preview
- Dated: gemini-2.5-flash-preview-09-2025, etc.

**Anthropic:**
- claude-3-haiku, claude-3-5-sonnet, claude-3-5-sonnet-v1, claude-3-7-sonnet
- claude-haiku-4-5, claude-sonnet-4, claude-sonnet-4-5, claude-sonnet-4-6
- Dated: claude-sonnet-4@20250514, etc.

**xAI:** grok-beta

**Other:**
- qwen3-4b, qwen3-30b-a3b
- glm-45-air-fp8
- custom-llm (for custom endpoints)

**Reasoning Effort Levels:** none, minimal, low, medium, high

### 5.5 ASR Config

```
quality: enum("high") - Only "high" available
provider: enum("elevenlabs"|"scribe_realtime") (default "elevenlabs")
user_input_audio_format: same as TTSOutputFormat (default "pcm_16000")
keywords: string[] - Boost prediction probability
```

### 5.6 Turn Detection Config

```
turn_timeout: number (default 7.0) - Max wait for user reply (seconds)
initial_wait_time: number|null - Wait for user to start (if no first_message)
silence_end_call_timeout: number (default -1) - Auto-end on silence (-1 = disabled)
soft_timeout_config: SoftTimeoutConfig
  timeout_seconds: number (default -1.0) - Filler message delay
  message: string (default "Hhmmmm...yeah.") - Filler text
  use_llm_generated_message: boolean (default false)
mode: enum("silence"|"turn") (default "turn")
turn_eagerness: enum("patient"|"normal"|"eager") (default "normal")
spelling_patience: enum("auto"|"off") (default "auto")
```

### 5.7 VAD Config

```
background_voice_detection: boolean (default false) - Background voice filtering
```

### 5.8 Conversation Config

```
text_only: boolean (default false) - Skip audio, text-only mode (cheaper)
max_duration_seconds: integer (default 600) - 10 min max
client_events: ClientEvent[] - Events sent to client
file_input: FileInputConfig
  enabled: boolean (default true)
  max_files_per_conversation: integer (1-10, default 10)
monitoring_enabled: boolean (default false) - Real-time monitoring via WebSocket
monitoring_events: ClientEvent[]
```

### 5.9 TTS Conversational Config

```
model_id: TTSConversationalModel (default "eleven_flash_v2")
voice_id: string (default "cjVigY5qzO86Huf0OWal")
supported_voices: SupportedVoice[] - Additional voices
  - label: string (min 1 char)
  - voice_id: string (min 0 chars)
expressive_mode: boolean (default true) - Audio tags for v3 models
suggested_audio_tags: SuggestedAudioTag[] (max 20)
  - tag: string (1-30 chars, e.g. "happy", "excited")
  - description: string|null (max 200 chars)
agent_output_audio_format: TTSOutputFormat (default "pcm_16000")
optimize_streaming_latency: 0-4 (default 3)
stability: number (0.0-1.0, default 0.5)
speed: number (0.8-1.2, default 1.0)
similarity_boost: number (0.0-1.0, default 0.75)
```

### 5.10 Built-in Tools

```
end_call: SystemToolConfig|null
language_detection: SystemToolConfig|null
transfer_to_agent: SystemToolConfig|null
transfer_to_number: SystemToolConfig|null
skip_turn: SystemToolConfig|null
play_keypad_touch_tone: SystemToolConfig|null (DTMF)
voicemail_detection: SystemToolConfig|null
```

**SystemToolConfig fields:**
```
type: "system" (const)
name: string (regex: ^[a-zA-Z0-9_-]{1,64}$)
description: string (default "") - Custom or auto-generated
response_timeout_secs: integer (default 20)
disable_interruptions: boolean (default false)
force_pre_tool_speech: boolean (default false)
assignments: DynamicVariableAssignment[]
tool_call_sound: ToolCallSoundType|null
tool_call_sound_behavior: ToolCallSoundBehavior
execution_mode: ToolExecutionMode
```

**Tool Call Sound Types:** typing, elevator1, elevator2, elevator3, elevator4  
**Sound Behavior:** auto, always  
**Execution Mode:** immediate, post_tool_speech, async

### 5.11 Dynamic Variables

```json
{
  "dynamic_variable_placeholders": {
    "user_name": "John Doe",
    "order_id": 12345
  }
}
```

**Dynamic Variable Assignments (from tool responses):**
```json
{
  "source": "response",
  "dynamic_variable": "user_name",
  "value_path": "user.name",     // Dot notation
  "sanitize": false              // Remove from transcript if true
}
```

### 5.12 Agent Transfer Config

```json
{
  "agent_id": "string (required)",
  "condition": "string (required) - When to transfer",
  "delay_ms": 0,
  "transfer_message": "string|null",
  "enable_transferred_agent_first_message": false,
  "is_workflow_node_transfer": false
}
```

### 5.13 Call Limits

```
agent_concurrency_limit: integer (default -1, unlimited)
daily_limit: integer (default 100,000)
bursting_enabled: boolean (default true) - Allow 3x burst at 2x rate
```

### 5.14 Platform Settings

```
evaluation: EvaluationSettings - Success criteria
widget: WidgetConfig - Embeddable widget
data_collection: { key: LiteralJsonSchemaProperty } - Extract structured data
data_collection_scopes: { key: AnalysisScope }
overrides: ConversationInitiationClientDataConfig - What clients can override
workspace_overrides: webhooks, initiation webhook
testing: AgentTestingSettings - Attached test IDs (max 1000)
archived: boolean (default false)
guardrails: GuardrailSettings
conversation_deletion: ConversationDeletionSettings
conversation_history_redaction: ConversationHistoryRedactionConfig
```

### 5.15 Guardrail Settings

Not fully exposed in spec beyond `GuardrailSettings-Input` reference.

### 5.16 PII Redaction Entities

Full list of redactable entity types:
```
name, name.name_given, name.name_family, name.name_other
email_address, contact_number, dob, age
religious_belief, political_opinion, sexual_orientation, ethnicity_race
marital_status, occupation, physical_attribute, language
username, password, url, organization
financial_id, financial_id.payment_card, .payment_card_number, .payment_card_expiration_date, .payment_card_cvv
financial_id.bank_account, .bank_account_number, .bank_routing_number, .swift_bic_code, .financial_id_other
location, .location_address, .location_city, .location_postal_code, .location_coordinate, .location_state, .location_country, .location_other
date, date_interval
unique_id, .government_issued_id, .account_number, .vehicle_id
unique_id.healthcare_number, .medical_record_number, .health_plan_beneficiary_number
unique_id.device_id, .unique_id_other
medical, ...
```

### 5.17 Conversation Deletion Settings

```
deletion_time_unix_secs: integer|null
deleted_logs_at_time_unix_secs: integer|null
deleted_audio_at_time_unix_secs: integer|null
deleted_transcript_at_time_unix_secs: integer|null
delete_transcript_and_pii: boolean (default false)
delete_audio: boolean (default false)
```

### 5.18 Coaching System

**CoachedAgentSettings:**
```
type: "coached" (const)
memory_base_id: string|null
```

**CoachingAgentSettings:**
```
type: "coaching" (const)
coached_agent_id: string (required)
memory_base_id: string|null
```
*Description: "Runtime-only settings for the virtual coach. Not stored in the database."*

---

## 6. Workflow/Graph Builder

### 6.1 Node Types

| Type | Const | Description |
|------|-------|-------------|
| `start` | `WorkflowStartNodeModel` | Entry point of workflow |
| `end` | `WorkflowEndNodeModel` | Terminates conversation |
| `phone_number` | `WorkflowPhoneNumberNodeModel` | Transfer to phone/SIP |
| `override_agent` | `WorkflowOverrideAgentNodeModel` | Subagent with overrides |
| `standalone_agent` | `WorkflowStandaloneAgentNodeModel` | Transfer to separate agent |
| `tool` | `WorkflowToolNodeModel` | Execute tools in parallel |

**All nodes share:**
```
position: { x: number, y: number }
edge_order: string[] - Outgoing edge evaluation order
```

### 6.2 Override Agent Node

```
conversation_config: ConversationalConfigAPIModelWorkflowOverride
additional_prompt: string - Added to system prompt
additional_knowledge_base: KnowledgeBaseLocator[]
additional_tool_ids: string[]
```

### 6.3 Standalone Agent Node

```
agent_id: string (required)
delay_ms: integer (default 0) - Transfer delay
transfer_message: string|null
enable_transferred_agent_first_message: boolean (default false)
```

### 6.4 Phone Number Node

```
transfer_destination: PhoneNumberTransferDestination | SIPUriTransferDestination
custom_sip_headers: (CustomSIPHeader | CustomSIPHeaderWithDynamicVariable)[] (max 20)
```

### 6.5 Tool Node

```
tools: WorkflowToolLocator[]
```
All tools execute in parallel. Node succeeds if all tools succeed.

### 6.6 Edge Conditions

Edges connect nodes with conditions:

| Condition Type | Const | Description |
|---------------|-------|-------------|
| `unconditional` | Always true | Default path |
| `llm` | LLM-evaluated | Natural language condition |
| `result` | Tool result | Based on tool success/failure |
| `expression` | Deterministic | AST-based expression |

**LLM Condition:**
```json
{
  "type": "llm",
  "label": "User asks about pricing",
  "condition": "User's last message contains a question about our pricing."
}
```

**Expression Condition (AST):**
Supports: String/Number/Boolean literals, LLM extraction, Dynamic variables,
AND/OR/Equals/NotEquals/GreaterThan/LessThan operators.

**Result Condition:**
```json
{
  "type": "result",
  "successful": true  // Did all tools succeed?
}
```

---

## 7. Widget Configuration

### 7.1 Widget Config

```
variant: enum("tiny"|"compact"|"full"|"expandable") (default "full")
placement: enum("top-left"|"top"|"top-right"|"bottom-left"|"bottom"|"bottom-right") (default "bottom-right")
expandable: enum("never"|"mobile"|"desktop"|"always") (default "never")
avatar: OrbAvatar | URLAvatar | ImageAvatar
feedback_mode: enum("none"|"during"|"end") (default "none")
end_feedback: WidgetEndFeedbackConfig|null
  type: enum("rating") (default "rating")
bg_color: string (default "#ffffff")
text_color: string (default "#000000")
btn_color: string (default "#000000")
btn_text_color: string (default "#ffffff")
border_color: string
focus_color: string
btn_radius: string
border_radius: string
sharp_corners: boolean (default false)
custom_css_styles: string|null
custom_css_url: string|null
mic_mode: enum - Microphone mode
styles: WidgetStyles - Full theme system
text_contents: WidgetTextContents - All UI strings
language_presets: { lang: WidgetLanguagePreset }
terms_text: string|null
terms_html: string|null
conversation_signature: string|null
```

### 7.2 Widget Styles (Full Theme)

```
base, base_hover, base_active, base_border, base_subtle, base_primary
```
Each is a CSS color string.

### 7.3 Widget Text Contents

All UI text is customizable:
```
main_label, start_call, start_chat, new_call, end_call
mute_microphone, unmute_microphone, send, type_message
starting_call, connecting, conversation_ended
speak_now, powered_by, feedback_question, listening
```
Plus many more i18n strings.

---

## 8. WebSocket / Client Events

### 8.1 Client Event Types

```
conversation_initiation_metadata  - Connection metadata
asr_initiation_metadata           - ASR config details
ping                              - Keep-alive
audio                             - Audio data chunk
interruption                      - User interrupted agent
user_transcript                   - Final user transcription
tentative_user_transcript         - Interim transcription
agent_response                    - Agent text response
agent_response_correction         - Corrected agent text
client_tool_call                  - Client-side tool invocation
mcp_tool_call                     - MCP tool invocation
mcp_connection_status             - MCP server connection state
agent_tool_request                - Agent requesting tool execution
agent_tool_response               - Tool execution result
agent_response_metadata           - Response metadata
vad_score                         - Voice activity detection score
agent_chat_response_part          - Streaming text part
client_error                      - Error from client
guardrail_triggered               - Guardrail activated
dtmf_request                      - DTMF tone request
internal_turn_probability         - Turn probability (internal)
internal_tentative_agent_response - Tentative response (internal)
```

### 8.2 Monitoring Events

Same event types, configured separately via `monitoring_events` array.
Enable with `monitoring_enabled: true`.

---

## 9. MCP Server Integration

### 9.1 Endpoints

| Method | Path | Operation |
|--------|------|-----------|
| POST | `/mcp-servers` | `create_mcp_server_route` |
| GET | `/mcp-servers` | `list_mcp_servers_route` |
| GET | `/mcp-servers/{id}` | `get_mcp_route` |
| DELETE | `/mcp-servers/{id}` | `delete_mcp_server_route` |
| PATCH | `/mcp-servers/{id}` | `update_mcp_server_config_route` |
| PATCH | `/mcp-servers/{id}/approval-policy` | `update_mcp_server_approval_policy_route` |
| POST | `/mcp-servers/{id}/tool-approvals` | `add_mcp_server_tool_approval_route` |
| DELETE | `/mcp-servers/{id}/tool-approvals/{tool}` | `remove_mcp_server_tool_approval_route` |
| POST | `/mcp-servers/{id}/tool-configs` | `add_mcp_tool_config_override_route` |
| GET | `/mcp-servers/{id}/tool-configs/{tool}` | `get_mcp_tool_config_override_route` |
| PATCH | `/mcp-servers/{id}/tool-configs/{tool}` | `update_mcp_tool_config_override_route` |
| DELETE | `/mcp-servers/{id}/tool-configs/{tool}` | `delete_mcp_tool_config_override_route` |
| GET | `/mcp-servers/{id}/tools` | `list_mcp_tools_route` |

### 9.2 MCP Server Config

```
transport: enum("SSE"|"STREAMABLE_HTTP") (default "SSE")
url: string | ConvAISecretLocator - MUST use https
secret_token: ConvAISecretLocator | ConvAIUserSecretDBModel | null
request_headers: { key: string | SecretLocator | DynamicVariable | EnvVarLocator }
auth_connection: AuthConnectionLocator|null
approval_policy: enum("auto_approve_all"|"require_approval_all"|"require_approval_per_tool")
tool_approval_hashes: MCPToolApprovalHash[]
```

### 9.3 MCP Tool Config Override

Per-tool overrides:
```
tool_name: string
force_pre_tool_speech: boolean|null
disable_interruptions: boolean|null
tool_call_sound: ToolCallSoundType|null
tool_call_sound_behavior: ToolCallSoundBehavior|null
execution_mode: ToolExecutionMode|null (immediate|post_tool_speech|async)
assignments: DynamicVariableAssignment[]
input_overrides: { key: ConvAISecretLocator | ConvAIEnvVarLocator | ConvAIDynamicVariable }
```

### 9.4 Tool Types (Full Enum)

```
system, webhook, client, mcp, workflow, api_integration_webhook, api_integration_mcp, smb
```

---

## 10. Telephony & Batch Calling

### 10.1 Phone Numbers

| Method | Path | Operation |
|--------|------|-----------|
| POST | `/phone-numbers` | Create phone number |
| GET | `/phone-numbers` | List phone numbers |
| GET | `/phone-numbers/{id}` | Get phone number |
| DELETE | `/phone-numbers/{id}` | Delete phone number |
| PATCH | `/phone-numbers/{id}` | Update phone number |

**Providers:** `twilio`, `sip_trunk`

### 10.2 Outbound Calls

| Method | Path | Operation |
|--------|------|-----------|
| POST | `/twilio/outbound-call` | Twilio outbound |
| POST | `/twilio/register-call` | Register Twilio call (returns TwiML) |
| POST | `/sip-trunk/outbound-call` | SIP trunk outbound |
| POST | `/whatsapp/outbound-call` | WhatsApp voice call |
| POST | `/whatsapp/outbound-message` | WhatsApp text message |

**Twilio Outbound Body:**
```
agent_id: string (required)
agent_phone_number_id: string (required)
to_number: string (required)
conversation_initiation_client_data: ConversationInitiationClientDataRequest|null
call_recording_enabled: boolean|null
```

**SIP Trunk Outbound Body:**
```
agent_id, agent_phone_number_id, to_number (all required)
conversation_initiation_client_data|null
telephony_call_config: { ringing_timeout_secs: 60 }
```

### 10.3 Batch Calling

| Method | Path | Operation |
|--------|------|-----------|
| POST | `/batch-calling/submit` | `create_batch_call` |
| GET | `/batch-calling/workspace` | `get_workspace_batch_calls` |
| GET | `/batch-calling/{id}` | `get_batch_call` |
| DELETE | `/batch-calling/{id}` | `delete_batch_call` |
| POST | `/batch-calling/{id}/cancel` | `cancel_batch_call` |
| POST | `/batch-calling/{id}/retry` | `retry_batch_call` |

**Batch Call Submit Body:**
```
call_name: string (required)
agent_id: string (required)
recipients: OutboundCallRecipient[] (max 10,000)
  - id: string|null
  - phone_number: string|null
  - whatsapp_user_id: string|null
  - conversation_initiation_client_data: {...}|null
scheduled_time_unix: integer|null
agent_phone_number_id: string|null
whatsapp_params: BatchCallWhatsAppParams|null
timezone: string|null
branch_id: string|null
environment: string|null
telephony_call_config: { ringing_timeout_secs: 1-999, default 60 }
target_concurrency_limit: integer|null (min 1)
```

### 10.4 WhatsApp

| Method | Path | Operation |
|--------|------|-----------|
| GET | `/whatsapp-accounts` | List WhatsApp accounts |
| GET | `/whatsapp-accounts/{id}` | Get account |
| PATCH | `/whatsapp-accounts/{id}` | Update account |
| DELETE | `/whatsapp-accounts/{id}` | Delete account |

---

## 11. Knowledge Base

### 11.1 Endpoints

| Method | Path | Operation |
|--------|------|-----------|
| POST | `/knowledge-base` | Add to KB (with optional agent_id) |
| GET | `/knowledge-base` | List all KB docs |
| POST | `/knowledge-base/file` | Create file document |
| POST | `/knowledge-base/folder` | Create folder |
| POST | `/knowledge-base/text` | Create text document |
| POST | `/knowledge-base/url` | Create URL document |
| POST | `/knowledge-base/bulk-move` | Bulk move to folder |
| GET | `/knowledge-base/summaries` | Bulk summaries by IDs |
| PATCH | `/knowledge-base/{id}` | Update document |
| GET | `/knowledge-base/{id}` | Get document |
| DELETE | `/knowledge-base/{id}` | Delete document |
| GET | `/knowledge-base/{id}/content` | Get document content |
| GET | `/knowledge-base/{id}/chunk/{chunk_id}` | Get specific chunk |
| GET | `/knowledge-base/{id}/dependent-agents` | Agents using this doc |
| POST | `/knowledge-base/{id}/refresh` | Re-fetch URL document |
| GET | `/knowledge-base/{id}/source-file-url` | Get source file URL |
| POST | `/knowledge-base/{id}/rag-index` | Create/check RAG index |
| GET | `/knowledge-base/{id}/rag-index` | List RAG indexes |
| DELETE | `/knowledge-base/{id}/rag-index/{rag_id}` | Delete RAG index |
| POST | `/knowledge-base/rag-index` | Batch RAG index creation |
| GET | `/knowledge-base/rag-index` | RAG index overview |
| GET | `/agent/{id}/knowledge-base/size` | Agent KB size |

---

## 12. Testing & Simulation

### 12.1 Agent Testing

| Method | Path | Operation |
|--------|------|-----------|
| GET | `/agent-testing` | List tests (paginated, searchable) |
| POST | `/agent-testing/create` | Create test (unit test or conversation test) |
| GET | `/agent-testing/{id}` | Get test |
| PUT | `/agent-testing/{id}` | Update test |
| DELETE | `/agent-testing/{id}` | Delete test |
| POST | `/agent-testing/summaries` | Bulk test summaries |
| POST | `/agent-testing/folders` | Create test folder |
| GET/PATCH/DELETE | `/agent-testing/folders/{id}` | Folder CRUD |
| POST | `/agent-testing/bulk-move` | Bulk move tests |

### 12.2 Test Invocations

| Method | Path | Operation |
|--------|------|-----------|
| GET | `/test-invocations` | List test runs |
| GET | `/test-invocations/{id}` | Get test run |
| POST | `/test-invocations/{id}/resubmit` | Re-run test |

### 12.3 Agent Test Execution

| Method | Path | Operation |
|--------|------|-----------|
| POST | `/agents/{id}/run-tests` | Run test suite on agent |

### 12.4 Conversation Simulation

| Method | Path | Operation |
|--------|------|-----------|
| POST | `/agents/{id}/simulate-conversation` | Simulate conversation |
| POST | `/agents/{id}/simulate-conversation/stream` | Stream simulation |

**Simulation Spec:**
```
simulated_user_config: AgentConfigAPIModel (required) - The "user" agent config
tool_mock_config: { tool_name: ToolMockConfig }
  - default_return_value: string (default "Tool Called.")
  - default_is_error: boolean (default false)
partial_conversation_history: ConversationHistoryTranscript[] - Resume from history
dynamic_variables: { key: string|number|boolean|null }
```

---

## 13. Environment Variables & Secrets

### 13.1 Environment Variables

| Method | Path | Operation |
|--------|------|-----------|
| POST | `/environment-variables` | Create env var |
| GET | `/environment-variables` | List env vars |
| GET | `/environment-variables/{id}` | Get env var |
| PATCH | `/environment-variables/{id}` | Update env var |

**Types:** `string`, `secret`, `auth_connection`

**Values are per-environment** (must include `production` key):
```json
{
  "type": "string",
  "label": "API_ENDPOINT",
  "values": {
    "production": "https://api.example.com",
    "staging": "https://staging.api.example.com"
  }
}
```

### 13.2 Secrets

| Method | Path | Operation |
|--------|------|-----------|
| POST | `/secrets` | Create secret |
| GET | `/secrets` | List secrets |
| DELETE | `/secrets/{id}` | Delete secret |
| PATCH | `/secrets/{id}` | Update secret |

### 13.3 Auth Connections (Workspace)

| Method | Path | Operation |
|--------|------|-----------|
| POST | `/workspace/auth-connections` | Create auth connection |
| GET | `/workspace/auth-connections` | List auth connections |
| DELETE | `/workspace/auth-connections/{id}` | Delete auth connection |

---

## 14. Hidden Gems & Intelligence

### 14.1 Deprecated/Legacy Endpoints

- `GET /v1/convai/conversation/get_signed_url` - Deprecated duplicate (underscore version)
- `eleven_turbo_v2` model - Deprecated, use `eleven_flash_v2`
- `eleven_turbo_v2_5` model - Deprecated, use `eleven_flash_v2_5`
- `send_audio` field on ConvAIWebhooks - Deprecated, use `events` array
- `optimize_streaming_latency` query param on some TTS endpoints - Deprecated

### 14.2 Custom Extension Fields (x-convai-*)

The spec uses custom OpenAPI extensions for client behavior:
- `x-convai-client-override: true` - Field can be overridden by client at connection time
- `x-convai-language-override: true` - Field can vary per language preset
- `x-fern-enum` - Used for SDK code generation (Fern framework)
- `x-fern-ignore: true` - Hidden from SDK generation

**Client-overridable fields:** first_message, language, voice_id, prompt, llm, tool_ids, stability, speed, similarity_boost, text_only, file_input, tool_call_sound, soft_timeout message

### 14.3 Internal/Unusual Endpoints

- `POST /v1/single-use-token/{token_type}` - Generate single-use tokens
- `POST /v1/forced-alignment` - Force-align audio to text (precise timing)
- `POST /v1/audio-native` - Audio native content embedding
- `POST /v1/music` - Music generation
- `POST /v1/music/detailed` - Detailed music generation
- `POST /v1/music/stem-separation` - Separate stems from audio
- `POST /v1/music/video-to-music` - Generate music from video
- `POST /v1/sound-generation` - Generate sound effects
- `GET /v1/service-accounts` - Service account management
- `POST /v1/studio/podcasts` - Podcast generation
- `GET /v1/workspace/webhooks` - Workspace-level webhooks

### 14.4 Interesting Constraints

- MCP servers per agent: **max 10**
- Batch call recipients: **max 10,000**
- Test attachments per agent: **max 1,000**
- Suggested audio tags: **max 20**
- Custom SIP headers: **max 20**
- Pronunciation dictionaries per request: **max 3**
- Max files per conversation: **1-10** (default 10)
- Voice name regex: `^[a-zA-Z0-9_-]{1,64}$`
- Max conversation duration: **600 seconds** (10 min) default
- Seed range: **0 to 4,294,967,295** (uint32)
- STT file max size: **3.0 GB**
- STT min audio: **100ms**
- Max speakers for STT: **32**
- Speed range: **0.7-1.2** (ConvAI) or broader for regular TTS
- Traffic percentage for deployments: **0-100**
- Ringing timeout: **1-999 seconds**

### 14.5 Conversation Initiation Sources

```
unknown, android_sdk, node_js_sdk, react_native_sdk, react_sdk, js_sdk,
python_sdk, widget, sip_trunk, twilio, genesys, swift_sdk, whatsapp,
flutter_sdk, zendesk_integration, slack_integration, template_preview
```

### 14.6 Webhook Events

```
transcript - Full conversation transcript
audio - Audio recording
call_initiation_failure - Failed to start call
```

### 14.7 Model Default: gemini-2.5-flash

The default LLM for new agents is `gemini-2.5-flash`, not Claude or GPT.

### 14.8 Default Voice ID

`cjVigY5qzO86Huf0OWal` - This is the default voice for new ConvAI agents.

### 14.9 Coaching System (Hidden Feature)

Two agent settings types suggest a coaching/training system:
- `CoachedAgentSettings` - An agent being coached, with a `memory_base_id`
- `CoachingAgentSettings` - A coach agent, references `coached_agent_id` with `memory_base_id`
- CoachingAgentSettings description: *"Runtime-only settings for the virtual coach. Not stored in the database."*

### 14.10 AST Expression Engine

The workflow expression conditions use a full AST (Abstract Syntax Tree):
- Literal nodes: String, Number, Boolean
- LLM extraction node: Schema-based value extraction
- Dynamic variable references
- Operators: AND, OR, Equals, NotEquals, GreaterThan, LessThan, Contains, OneOf, InRange, IsNull, Not

### 14.11 Data Residency

The LLM description mentions: *"If using data residency, the LLM must be supported in the data residency environment"* - suggesting EU/region-specific deployment options.

### 14.12 LiveKit Integration

The `get_livekit_token` endpoint confirms ElevenLabs uses **LiveKit** as their real-time communication infrastructure for ConvAI.

### 14.13 Genesys & Zendesk Integrations

`ConversationInitiationSource` includes `genesys` and `zendesk_integration`, confirming enterprise contact center integrations.

### 14.14 Multivoice Messages

Conversation transcripts support `multivoice_message` with parts, suggesting multi-speaker conversation support within a single agent turn.

### 14.15 Conversation Turn Metrics

Per-turn metrics tracking:
```
convai_asr_provider: string - Which ASR was used
convai_tts_model: string - Which TTS model
convai_tts_cascade: string - TTS cascade info
metrics: { key: MetricRecord }
```

### 14.16 Other Endpoints of Interest

| Category | Count | Path Pattern |
|----------|-------|-------------|
| Dubbing | 17 | `/v1/dubbing/*` |
| Studio/Projects | 17 | `/v1/studio/*` |
| Music | 7 | `/v1/music/*` |
| Workspace | 12 | `/v1/workspace/*` |
| History | 4 | `/v1/history/*` |
| Models | 1 | `/v1/models` |
| Pronunciation | 7 | `/v1/pronunciation-dictionaries/*` |
| Usage | 1 | `/v1/usage/character-stats` |
| User | 2 | `/v1/user`, `/v1/user/subscription` |
| Audio Isolation | 2 | `/v1/audio-isolation`, `/v1/audio-isolation/stream` |

### 14.17 Custom LLM Support

The `custom-llm` option in the LLM enum + `custom_llm_extra_body` field enables bringing your own LLM endpoint to ConvAI agents.

### 14.18 Hinglish Mode

Dedicated `hinglish_mode: boolean` - When language is Hindi, agent responds in Hinglish (Hindi-English mix). This suggests significant India market focus.

---

## Appendix: Schema Count by Category

| Category | Approx Count |
|----------|-------------|
| Agent/ConvAI | ~200 |
| Workflow/Graph | ~40 |
| TTS/Voice | ~80 |
| Knowledge Base | ~30 |
| Testing | ~25 |
| MCP Server | ~20 |
| Telephony | ~30 |
| Widget | ~25 |
| Conversation History | ~50 |
| Dubbing | ~40 |
| Studio | ~30 |
| Other | ~470 |
| **Total** | **1,039** |
