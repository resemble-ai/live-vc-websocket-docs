# Resemble Live Voice Conversion API

This document describes how to connect to the Resemble real-time voice conversion websocket server, send microphone audio, and receive converted audio back.

**Current API version: `2.0.0`.** See [API version](#api-version-api_version) for the compatibility rules.

> **Message format.** Every JSON message — in both directions — uses the envelope
> `{"type": "<string>", "data": { ... }}`. Audio travels as binary frames (see
> [Stream Audio](#3-stream-audio)).

---



## Quick Start (Sample Code)
We have provided `client.py` as a reference implementation of connecting and streaming to our server via websockets. To use, clone this repository and run
```
uv sync
uv run client.py --api-key {API-KEY} --server https://{SERVER-DOMAIN} 
```
If we provided a username and password, pass those as well with `--basic-user {USERNAME} --basic-pass {PASSWORD}`


## Getting Started 
There are three steps to get audio flowing:

1. **Authenticate** -- exchange your API key for a connection ticket
2. **Connect & configure** -- open a WebSocket (the server greets you with a `session` message), pick a voice, and set your audio parameters
3. **Stream** -- send audio chunks, receive converted audio chunks

---

## 1. Authenticate

Before opening a WebSocket, request a short-lived connection ticket. You will need your **API key** for this step. If we also provided you with a **username and password**, you will need those too.

**Request:**

```
POST https://<host>/api/auth/ticket
```

**Headers:**

- `X-Api-Key` -- your API key (required)

**Basic auth (if applicable):**

If we gave you a username and password, include them as HTTP Basic Authentication on this request. Most HTTP libraries support this natively (e.g. the `auth` parameter in Python's `requests`, or the `Authorization: Basic <base64>` header).

**Response:**

```json
{ "ticket": "abc123..." }
```

Tickets are single-use and expire after 30 seconds. If the API key is invalid, the server responds with HTTP 403.

---

## 2. Connect & Configure

### Open the WebSocket

Connect to the server with your ticket and declare your API version:

```
wss://<host>/ws?ticket=<ticket>&api_version=2.0.0
```

If we gave you a username and password, embed them in the URL:

```
wss://<username>:<password>@<host>/ws?ticket=<ticket>&api_version=2.0.0
```

If the ticket is invalid, expired, or already used, the connection is rejected with close code `4003`.

#### API version (`api_version`)

The API uses semantic versioning (`major.minor.patch`). **Compatibility is gated on the major version only** — a client and server with the same major are compatible; minor and patch differences are backwards-compatible bookkeeping and never cause a rejection.

- Pass your version in the `api_version` query param (e.g. `2.0.0`, or just `2`).
- **You must declare a supported major.** Omitting the param makes the server assume the legacy `1.0.0`, which is **no longer supported** — so the connection is rejected. Always send `api_version=2.0.0`.
- If the server doesn't support your major version, it sends an [`error`](#server-messages-reference) message with `code: "unsupported_api_version"` and closes the connection with close code **`4426`** (mnemonic for HTTP 426 "Upgrade Required"). This is a clean, predictable failure — treat it as "upgrade your client."

The current server version and the majors it accepts are reported in the [`session`](#the-session-handshake) message and the [`settings`](#querying-server-info) response (`api_version`, `supported_api_majors`).

### The `session` handshake

As soon as the connection is accepted, **the server sends a `session` message** — you don't request it. It carries your session identity and everything you need to get started:

```json
{
  "type": "session",
  "data": {
    "session_id": "5f3c…",
    "api_version": "2.0.0",
    "supported_api_majors": [2],
    "slot": 0,
    "settings": { "client_input_sr": 48000, "chunk_samples": 5760, "vad": 2, "extra_convert_size": 32784, "output_sample_rate": 48000, "vc_enabled": true },
    "capabilities": {
      "voices": ["voice_a", "voice_b"],
      "current_voice": "voice_a",
      "output_sample_rate": 48000,
      "native_output_sample_rate": 48000
    }
  }
}
```

- `session_id` — opaque identifier for this connection. Quote it when reporting issues.
- `slot` — opaque pool index for this session (not a hardware identity).
- `settings` — the current effective settings (see [Apply Settings](#apply-settings)).
- `capabilities.voices` — available voice models; `capabilities.output_sample_rate` — the Hz of audio the server returns; `capabilities.native_output_sample_rate` — the voice model's native rate (what you get if you don't request a specific `output_sample_rate`).
- `deprecation` — **present only if your API version is being phased out** (see below). Absent means your version is current.

#### Deprecation notices

Compatibility is gated on the **major** version, but within a supported major an older version can be *soft-deprecated*: it keeps working, but you're told to upgrade and by when. When that applies to your declared `api_version`, the `session` handshake includes a `deprecation` object:

```json
"deprecation": {
  "deprecated": true,
  "your_version": "2.0.0",
  "min_supported_version": "2.1.0",
  "current_version": "2.1.0",
  "message": "API version 2.0.0 is deprecated. Upgrade to 2.1.0 or newer to avoid disruption.",
  "sunset": "2026-12-31",
  "removed_in": "3.0.0",
  "info_url": "https://.../migrating-to-2.1"
}
```

| Field | Meaning |
| --- | --- |
| `deprecated` | Always `true` when the object is present |
| `your_version` | The `api_version` you connected with |
| `min_supported_version` | The lowest version that is **not** deprecated — upgrade to this or newer |
| `current_version` | The server's latest version |
| `message` | Human-readable summary, safe to log/surface to operators |
| `sunset` | *(optional)* ISO-8601 date on/after which your version stops being served |
| `removed_in` | *(optional)* the server version that drops support for yours |
| `info_url` | *(optional)* link to a migration guide |

Your connection is **not** rejected — everything still works. Treat this as a signal to schedule an upgrade. A robust client logs the `message` (and alerts if `sunset` is near). This mirrors the HTTP [`Deprecation`/`Sunset`](https://www.rfc-editor.org/rfc/rfc8594) header convention. Once your version is actually removed, you'll instead be rejected at connect with `unsupported_api_version` / close code `4426`.

### Choose a Voice

The voice list arrives in the `session` handshake (`capabilities.voices`). You can also re-request it at any time:

```json
{ "type": "get_voices" }
```

The server responds with:

```json
{
  "type": "voices",
  "data": { "voices": ["voice_a", "voice_b", "voice_c"] }
}
```

### Apply Settings

Send your desired configuration. All fields are optional -- only include what you want to change.

```json
{
  "type": "update_settings",
  "data": {
    "voice": "voice_a",
    "chunk_samples": 5760,
    "client_input_sr": 48000,
    "extra_convert_size": 32784,
    "output_sample_rate": 48000,
    "vad": 2
  }
}
```

The server confirms with `{"type": "settings_updated", "data": {"settings": { ... }}}`. If you changed the voice, expect a `model_switching` message followed by `model_ready` before the confirmation.

Updates are **atomic and validated**: if any field is invalid (wrong type, out of range, or an unknown key), the server applies *nothing* and replies with an [`error`](#error-codes) of `code: "invalid_settings"` whose `data.details.fields` lists the offending fields.

**Available settings:**

| Field | Type | Range | Recommended | Description |
| --- | --- | --- | --- | --- |
| `voice` | string | | | Voice model to use |
| `chunk_samples` | int | ≥ 1280 | `5760` | Audio samples per chunk. `5760` = 120ms at 48kHz. Values below `1280` are rejected (per-chunk overhead dominates). |
| `client_input_sr` | int | ≥ 1 | `48000` | Your input audio sample rate in Hz |
| `extra_convert_size` | int | ≥ 0 | `32784` | Extra context for conversion quality. Higher = better quality, more latency. |
| `output_sample_rate` | int | 8000…48000 | *(native)* | Sample rate of the audio the server returns. Defaults to the voice model's native rate (`capabilities.native_output_sample_rate`). |
| `vad` | int | 0…3 | `2` | Voice activity detection. `0` = off, `1` = low, `2` = medium, `3` = high. |
| `vc_enabled` | bool | | `true` | Set to `false` for audio passthrough (useful for testing your connection). |

### Start the Stream

Tell the server you're ready to begin:

```json
{
  "type": "stream_start",
  "data": {
    "chunk_samples": 5760,
    "extra_convert_size": 32784
  }
}
```

The server may need a moment to prepare the pipeline. If so, it sends `warmup_start`, then `warmup_complete` when ready. If the pipeline is already prepared, it sends `warmup_complete` immediately.

**Wait for `warmup_complete` before sending audio.** Any audio sent during warmup is dropped.

---

## 3. Stream Audio

### Sending Audio

Send audio as **binary** WebSocket messages with this layout:

| Offset | Size | Type | Description |
| --- | --- | --- | --- |
| 0 | 8 bytes | float64 (little-endian) | Your current timestamp in milliseconds |
| 8 | N bytes | int16 array (little-endian) | Mono PCM audio samples |

Each message should contain exactly `chunk_samples` samples at the `client_input_sr` sample rate.

The timestamp can be any millisecond clock (e.g. Unix time in ms). It is echoed back in the response so you can measure round-trip latency.

### Receiving Audio

The server returns converted audio as **binary** WebSocket messages:

| Offset | Size | Type | Description |
| --- | --- | --- | --- |
| 0 | 4 bytes | uint32 (little-endian) | Length of the JSON header in bytes |
| 4 | M bytes | UTF-8 string | JSON header |
| 4 + M | N bytes | int16 array (little-endian) | Mono PCM audio samples |

**JSON header:**

```json
{
  "type": "audio_response",
  "data": {
    "timestamp": 1707600000000.0,
    "latency": {
      "total": 65.2,
      "inference": 64.8,
      "decode": 0.1,
      "normalize": 0.1,
      "denormalize": 0.1,
      "encode": 0.1
    }
  }
}
```

- `data.timestamp` -- your original timestamp, echoed back (use it to measure round-trip latency)
- `data.latency.total` -- total server processing time in ms
- `data.latency.inference` -- model inference time in ms

The output audio sample rate is reported as `capabilities.output_sample_rate` in the `session` handshake and the `settings` response (typically 48000 Hz). By default the server returns audio at the voice model's native rate; set the `output_sample_rate` setting (see [Apply Settings](#apply-settings)) if you need a specific rate.

### Stopping

When you're done, send:

```json
{ "type": "stream_stop" }
```

The server acknowledges with `{"type": "stream_stopped", "data": {}}`. Then close the WebSocket connection normally.

---

## Server Messages Reference

During a session, the server may send the following messages (all use the `{type, data}` envelope):

| Message | When | What It Means |
| --- | --- | --- |
| `session` | On connect | Handshake: `session_id`, `slot`, current `settings`, and `capabilities`. Sent automatically. |
| `voices` | After `get_voices` | `data.voices` — list of available voice models |
| `settings` | After `get_settings` | `data.settings` + `data.capabilities` + `data.api_version` / `data.supported_api_majors` |
| `settings_updated` | After `update_settings` | `data.settings` — the effective settings after the update |
| `model_switching` | When voice changes | A new voice model is loading. Audio is dropped until `model_ready`. |
| `model_ready` | After model loads | The new voice model is loaded and ready |
| `warmup_start` | Before first audio | The server is preparing the inference pipeline |
| `warmup_complete` | Pipeline ready | Safe to begin sending audio |
| `stream_stopped` | After `stream_stop` | Acknowledges your stop request |
| `error` | On failure | `data.code` (machine-readable), `data.message` (human), optional `data.details`. See [Error codes](#error-codes). |

### Error codes

Every error is `{"type": "error", "data": {"code", "message", "details"?}}`. The `code` is stable; the `message` is for humans and may change.

| `code` | Meaning |
| --- | --- |
| `unsupported_api_version` | Your `api_version` major isn't supported. Connection is closed (`4426`). |
| `server_at_capacity` | All conversion slots are busy. `data.details.retry_after` suggests seconds to wait. Connection is closed (`4029`). |
| `invalid_settings` | An `update_settings` was rejected; `data.details.fields` lists the bad fields. No changes were applied. |
| `model_load_failed` | A voice failed to load. |
| `unknown_message` | The server didn't recognize a message `type`. |
| `internal_error` | Unexpected server-side error. |

### WebSocket close codes

| Code | Meaning |
| --- | --- |
| `1001` | Server is shutting down (standard "going away"). Reconnect — don't treat it as an error. |
| `4003` | Invalid / expired ticket (mnemonic: HTTP 403) |
| `4029` | Server at capacity (mnemonic: HTTP 429) |
| `4426` | Unsupported API version (mnemonic: HTTP 426 Upgrade Required) |

---

## Querying Server Info

The `session` handshake already gives you voices, settings, and capabilities on connect. You can also re-query at any time:

| Send | Receive | Description |
| --- | --- | --- |
| `{ "type": "get_voices" }` | `voices` | List of available voice models (`data.voices`) |
| `{ "type": "get_settings" }` | `settings` | Current settings + capabilities |

The `settings` response (`data`) includes:
- `settings` — current effective settings (same shape as the session handshake)
- `capabilities.output_sample_rate` — the sample rate of audio the server sends back
- `capabilities.native_output_sample_rate` — the voice model's native output rate
- `api_version` — the server's current full version (e.g. `2.0.0`)
- `supported_api_majors` — the list of major versions the server accepts (e.g. `[2]`)

### HTTP endpoints

A couple of plain HTTP endpoints are available without a ticket — useful for monitoring and for checking capacity before you connect:

| Endpoint | Method | Returns |
| --- | --- | --- |
| `/health` | GET | `{ "status": "ok" }` — liveness/readiness probe |
| `/api/capacity` | GET | `{ "total", "available" }` — total concurrent conversion slots, and how many are free right now |

The protocol version the server speaks is in the [`session`](#the-session-handshake) handshake (`data.api_version`), so you don't need a separate call for it.

`total` is the number of simultaneous conversions the service can run; `available` is how many of those are currently free. Poll `/api/capacity` before connecting: if it reports `available: 0`, a WebSocket connection will be rejected with close code `4029` (see [WebSocket close codes](#websocket-close-codes)) — back off and retry using the `retry_after` hint.

---

## Recommended Configuration

These defaults provide a good balance of quality and latency for most use cases:

| Setting | Value | Notes |
| --- | --- | --- |
| `client_input_sr` | `48000` | 48kHz input. Matches most browser and hardware defaults. |
| `chunk_samples` | `5760` | 120ms chunks at 48kHz. Smaller chunks reduce latency but increase network overhead. Larger chunks can help cope with a bad connection. |
| `extra_convert_size` | `32784` | ~2 seconds of extra context. Produces the best conversion quality. Note this does *not* add 2 seconds of latency.  |
| `vad` | `2` | Medium voice activity detection. Reduces artifacts during silence. |