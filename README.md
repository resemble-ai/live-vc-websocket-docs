# Resemble Live Voice Conversion API

This document describes how to connect to the Resemble real-time voice conversion websocket server, send microphone audio, and receive converted audio back.

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
2. **Connect & configure** -- open a WebSocket, pick a voice, and set your audio parameters
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

Connect to the server with your ticket:

```
wss://<host>/ws?ticket=<ticket>
```

If we gave you a username and password, embed them in the URL:

```
wss://<username>:<password>@<host>/ws?ticket=<ticket>
```

If the ticket is invalid, expired, or already used, the connection is rejected with close code `4003`.

### Choose a Voice

Send a text message to list available voices:

```json
{ "type": "get_voices" }
```

The server responds with:

```json
{
  "type": "voices",
  "data": ["voice_a", "voice_b", "voice_c"]
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
    "vad": 2
  }
}
```

The server confirms with a `settings_updated` message. If you changed the voice, expect a `model_switching` message followed by `model_ready` before the confirmation.

**Available settings:**

| Field | Type | Recommended | Description |
| --- | --- | --- | --- |
| `voice` | string |  | Voice model to use |
| `chunk_samples` | int | `5760` | Number of audio samples per chunk. `5760` = 120ms at 48kHz. |
| `client_input_sr` | int | `48000` | Your input audio sample rate in Hz |
| `extra_convert_size` | int | `32784` | Extra context for conversion quality. Higher = better quality, more latency. |
| `vad` | int | `2` | Voice activity detection. `0` = off, `1` = low, `2` = medium, `3` = high. |
| `vc_enabled` | bool | `true` | Set to `false` for audio passthrough (useful for testing your connection). |
| `gpu` | int | `0` | GPU device index |

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
```

- `timestamp` -- your original timestamp, echoed back
- `latency.total` -- total server processing time in ms
- `latency.inference` -- model inference time in ms

The output audio sample rate is provided in the `actual_output_sr` field of the settings response (typically 48000 Hz).

### Stopping

When you're done, send:

```json
{ "type": "stream_stop" }
```

Then close the WebSocket connection normally.

---

## Server Messages Reference

During a session, the server may send the following messages:

| Message | When | What It Means |
| --- | --- | --- |
| `voices` | After `get_voices` | List of available voice models |
| `gpus` | After `get_gpus` | List of available GPUs with id, name, and memory |
| `settings` | After `get_settings` | Full current configuration including `actual_output_sr` and `current_voice` |
| `settings_updated` | After `update_settings` | Confirmation that your settings were applied |
| `model_switching` | When voice changes | A new voice model is loading. Audio is dropped until `model_ready`. |
| `model_ready` | After model loads | The new voice model is loaded and ready |
| `warmup_start` | Before first audio | The server is preparing the inference pipeline |
| `warmup_complete` | Pipeline ready | Safe to begin sending audio |
| `error` | On failure | Contains a `message` field describing what went wrong |

---

## Querying Server Info

These can be sent at any time during a connection:

| Send | Receive | Description |
| --- | --- | --- |
| `{ "type": "get_voices" }` | `voices` | List of available voice models |
| `{ "type": "get_gpus" }` | `gpus` | Available GPU devices |
| `{ "type": "get_settings" }` | `settings` | Current server configuration |

The `settings` response includes `actual_output_sr` (the sample rate of audio the server sends back) and `index_disabled` (whether speaker feature matching is available on this deployment).

---

## Recommended Configuration

These defaults provide a good balance of quality and latency for most use cases:

| Setting | Value | Notes |
| --- | --- | --- |
| `client_input_sr` | `48000` | 48kHz input. Matches most browser and hardware defaults. |
| `chunk_samples` | `5760` | 120ms chunks at 48kHz. Smaller chunks reduce latency but increase network overhead. Larger chunks can help cope with a bad connection. |
| `extra_convert_size` | `32784` | ~2 seconds of extra context. Produces the best conversion quality. Note this does *not* add 2 seconds of latency.  |
| `f0_up` | `0` | Let the server handle pitch matching automatically. We highly recommend keeping this at 0 |
| `vad` | `2` | Medium voice activity detection. Reduces artifacts during silence. |