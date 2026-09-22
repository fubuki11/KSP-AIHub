# AI Hub HTTP API v1

Default endpoint: `http://127.0.0.1:18181`. Every `/v1/*` call requires `Authorization: Bearer <IPC token>` and the exact loopback Host. Browser Origin and redirects are rejected.

## Endpoints

0.3.0 generation limits, typed output errors and bounded recovery are documented in [GENERATION.md](GENERATION.md). They are additive API v1 features; older generation requests remain valid.

| Method | Path | Meaning |
| --- | --- | --- |
| GET | `/v1/health` | Service/API version and readiness |
| GET | `/v1/profiles` | Profile/model/auth capability summaries; no secrets |
| POST | `/v1/profiles` | Add a preset or custom API-key connection without overwriting existing profiles |
| GET | `/v1/models?profile=<id>&refresh=true` | Fetch current model IDs; optional refresh bypasses the five-minute cache |
| GET | `/v1/ui` | Global default, enabled profiles and discovered consumer IDs |
| GET | `/v1/ui?clientId=org.vendor.ExampleMod` | Optional consumer's effective route and override/inheritance status |
| POST | `/v1/select` | Save a client's profile/model selection for future requests |
| POST | `/v1/generate` | Produce text or a JSON object using a request snapshot |
| POST | `/v1/auth/begin` | Start an enabled, registered OAuth + PKCE flow |
| POST | `/v1/auth/api-key` | Store an API key for an existing stored-key credential |
| GET | `/oauth/callback` | Browser callback; no IPC token, one-time state required |

`/v1/select` supports explicit scopes:

- Global default: `{scope:"global", profile, model}`. No client ID is required or accepted in global scope.
- Consumer override: `{scope:"client", clientId, profile, model}`.
- Clear a consumer override: `{scope:"client", clientId, inherit:true}`. Future requests follow subsequent global changes; a configured `clients` entry is also suppressed until explicitly overridden again.
- Reset the global runtime selection: `{scope:"global", inherit:true}`. Falls back to the configuration's `defaultProfile` and that profile's model.
- Legacy `{clientId, profile, model}` requests retain their per-client behavior. Omitted scope and omitted client ID select global scope.

`model` may be omitted/null to use the chosen profile's model; `inherit:true` cannot include profile/model. Model selection never changes credential bindings or endpoints.

Routing order: explicit request profile/model > consumer override > runtime global default > configuration default. Overrides are profile/model pairs: an explicit request for a different profile uses that profile's model unless the request also supplies a model. Missing/disabled selected profiles fail rather than silently rerouting to a different provider.

UI fields include `scope`, nullable `clientId`, `routingReady`, `selectionSource`, `hasOverride`, `inheritsGlobal`, `clientIds`, `profileIds` and `profileLabels`. An unconfigured route returns `routingReady:false` while retaining the profile choices so the manager can configure it.

`/v1/generate`:

```json
{
  "clientId": "org.vendor.ExampleMod",
  "profile": "openai-main",
  "model": "your-model-id",
  "format": "json",
  "messages": [
    {"role":"system","content":"Return a JSON craft plan."},
    {"role":"user","content":"Design request and context."}
  ]
}
```

Response: `{ok, requestId, profile, provider, model, text, jsonText, inputTokens, outputTokens}`. `jsonText` is empty for text mode and contains validated JSON-object text for JSON mode. `-1` token counts mean not reported by the provider; no cost is invented.

`clientId` is optional for generation. Omitted/null uses global routing directly; an explicitly empty or malformed ID is rejected. Any valid unregistered ID inherits global settings. Client IDs are case-sensitive routing labels, not API credentials or OAuth client IDs. Two consumers sharing an ID share that ID's override.

Errors: `{ok:false, code, message, requestId}` with an appropriate HTTP status. Typical codes include `unauthorized`, `invalid_request`, `profile_unavailable`, `auth_missing`, `auth_unsupported`, `oauth_registration_missing`, `provider_error`, `invalid_model_output`, `busy` and `timeout`.

The service does not run model tools or game commands. No automatic provider fallback or generation retry occurs. Requests are size-bounded, concurrency-bounded and provider-timeout-bounded. Closing a synchronous consumer connection does not guarantee cancellation of a request already accepted by the provider; full job leases/cancellation are a later protocol capability.

## Game SDK contract

0.2.0 adds model discovery and connection onboarding, without changing selection/generation requests:

- `POST /v1/profiles`: `{preset,id,baseUrl?,protocol?,modelsFormat?}`. Presets: `openai`, `anthropic`, `deepseek`, `mimo`, `gemini`, `zen`, `custom`. Only `custom` accepts a supplied base URL. Returns `{ok,profile}`; adding a profile does not automatically change any route. New IDs are unique, at most 64 characters. Managed connection data is persisted separately from `hub.json`; credentials remain encrypted.
- `GET /v1/models`: requires an enabled profile ID. Returns flat arrays `modelIds`, `modelLabels`, `modelProtocols`, plus `profile`, `modelsStatus`, `modelsMessage`, `cached`, `fetchedAt` (Unix seconds). `refresh` accepts `true`/`false`. Discovery failures return `ok:true` with a non-`ready` `modelsStatus` and empty arrays, so the UI can retain manual selection; invalid requests still return HTTP errors. No model is automatically selected.
- `/v1/ui` adds `presetIds`, `presetLabels`, `baseUrl`, `protocol`. The SDK adds `GetModelsAsync` and `AddProfileAsync`.
- Provider listing requests send credentials only to the configured API origin, reject redirects, handle supported pagination, and cap pages (20), entries (5000), response sizes and concurrent listing requests (2). No provider inference is invoked to discover a model.
- `modelsUrl` and `modelsFormat` are optional profile configuration fields. Formats are `openai`, `anthropic`, `gemini`, `none`. `none` keeps a manual-only endpoint. New API-key credentials may set `keyHeader`; OAuth credentials retain their existing header behavior.

`AiHubClient` takes a runtime connection file and an optional client ID. With no ID, `GetUiAsync` and `SelectAsync` operate on global defaults; with an ID they operate on that consumer. `InheritDefaultsAsync` clears the corresponding override. It can call `GenerateAsync` with cancellation of the local HTTP operation. Consumers should dispatch result handling onto Unity's main thread and perform their own validation before applying game state.

Runtime connection file: `{endpoint, token}`. This is an IPC credential, not a provider API key. Keep it in local PluginData; do not distribute it in archives.

## Compatibility

The base implementation supports text messages with roles system/user/assistant and JSON-object output. Tool calls, images, schema-constrained generation and outward SSE are not implicitly supported. Clients must not infer capabilities from a model name.

0.1.1 keeps API v1 compatible with existing identified callers. Runtime selection storage is `{schemaVersion:2,global:<selection-or-null>,clients:{id:<selection-or-null>}}`. Old flat client maps are read without rewriting and migrated on the next explicit save. Null client entries mean inherit global even if a configuration-file override exists. Existing legacy overrides are preserved, not automatically removed.
