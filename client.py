"""
Example client for real-time voice conversion via the Resemble WebSocket API.

Connects to the server, selects a voice model, and streams audio from your
microphone through the voice conversion pipeline to your speakers.

Usage:
    python client.py --server https://your-server.com --api-key YOUR_KEY [ (if provided) --basic-user USERNAME --basic-pass PASSWORD]

See README.md for full protocol documentation.
"""
import argparse
import asyncio
import json
import struct
import sys
import time
from datetime import datetime
from math import gcd

import numpy as np
import requests
from scipy.signal import resample_poly
import sounddevice as sd
import websockets


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts():
    """Compact timestamp for log lines."""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def _pick_device(kind: str) -> int:
    """Prompt user to pick an audio device of the given kind ('input' or 'output')."""
    devices = sd.query_devices()
    candidates = []
    for i, d in enumerate(devices):
        if kind == "input" and d["max_input_channels"] > 0:
            candidates.append((i, d))
        elif kind == "output" and d["max_output_channels"] > 0:
            candidates.append((i, d))

    if not candidates:
        print(f"No {kind} devices found!")
        sys.exit(1)

    print(f"\n{'=' * 50}")
    print(f"  Available {kind} devices:")
    print(f"{'=' * 50}")
    for idx, (dev_id, d) in enumerate(candidates):
        sr = int(d["default_samplerate"])
        print(f"  [{idx}] {d['name']}  (id={dev_id}, {sr}Hz)")

    while True:
        try:
            choice = int(input(f"\nSelect {kind} device [0-{len(candidates)-1}]: "))
            if 0 <= choice < len(candidates):
                dev_id, d = candidates[choice]
                print(f"  -> {d['name']}")
                return dev_id
        except (ValueError, EOFError):
            pass
        print("  Invalid choice, try again.")


def _get_ticket(server: str, api_key: str, basic_auth: tuple = None) -> str:
    """Exchange an API key for a single-use WebSocket connection ticket."""
    url = f"{server.rstrip('/')}/api/auth/ticket"
    headers = {"X-Api-Key": api_key} if api_key else {}
    resp = requests.post(url, headers=headers, auth=basic_auth, timeout=10)
    resp.raise_for_status()
    return resp.json()["ticket"]



def _resample(audio: np.ndarray, from_sr: int, to_sr: int) -> np.ndarray:
    """Resample audio using a polyphase FIR filter (scipy)."""
    if from_sr == to_sr or len(audio) == 0:
        return audio
    g = gcd(from_sr, to_sr)
    return resample_poly(audio, to_sr // g, from_sr // g).astype(audio.dtype)


# ---------------------------------------------------------------------------
# Streaming session
# ---------------------------------------------------------------------------

async def stream(
    server: str,
    ticket: str,
    input_dev: int,
    output_dev: int,
    sample_rate: int,
    chunk_samples: int,
    voice: str,
    extra_convert_size: int,
    basic_auth: tuple = None,
):
    """Connect to the server and stream audio bidirectionally."""

    # Build WebSocket URL
    ws_scheme = "wss" if server.startswith("https") else "ws"
    ws_host = server.replace("https://", "").replace("http://", "").rstrip("/")
    if basic_auth:
        user, passwd = basic_auth
        ws_url = f"{ws_scheme}://{user}:{passwd}@{ws_host}/ws?ticket={ticket}"
    else:
        ws_url = f"{ws_scheme}://{ws_host}/ws?ticket={ticket}"

    print(f"[{_ts()}] Connecting...")
    async with websockets.connect(ws_url, max_size=2**20) as ws:
        print(f"[{_ts()}] Connected.")

        # ---- Voice selection ----
        await ws.send(json.dumps({"type": "get_voices"}))
        msg = json.loads(await ws.recv())
        voices = msg.get("data", [])

        if voice and voice in voices:
            selected_voice = voice
            print(f"[{_ts()}] Using voice: {selected_voice}")
        elif voices:
            print(f"\n{'=' * 50}")
            print(f"  Available voices:")
            print(f"{'=' * 50}")
            for idx, v in enumerate(voices):
                print(f"  [{idx}] {v}")
            loop = asyncio.get_event_loop()
            while True:
                try:
                    raw = await loop.run_in_executor(
                        None, lambda: input(f"\nSelect voice [0-{len(voices)-1}]: ")
                    )
                    choice = int(raw)
                    if 0 <= choice < len(voices):
                        selected_voice = voices[choice]
                        print(f"  -> {selected_voice}")
                        break
                except (ValueError, EOFError):
                    pass
                print("  Invalid choice, try again.")
        else:
            print("No voices available on server.")
            return

        # ---- Apply settings ----
        settings = {
            "voice": selected_voice,
            "chunk_samples": chunk_samples,
            "client_input_sr": sample_rate,
            "extra_convert_size": extra_convert_size,
            "f0_up": 0,
            "vad": 2,
        }
        print(f"[{_ts()}] Configuring...")
        await ws.send(json.dumps({"type": "update_settings", "data": settings}))

        while True:
            msg = json.loads(await ws.recv())
            mtype = msg.get("type")
            if mtype == "model_switching":
                print(f"[{_ts()}] Loading model '{msg['data']['target_voice']}'...", end="", flush=True)
            elif mtype == "model_ready":
                print(" done.")
            elif mtype == "settings_updated":
                print(f"[{_ts()}] Settings applied.")
                break
            else:
                pass  # drain other messages

        # ---- Query server output sample rate ----
        await ws.send(json.dumps({"type": "get_settings"}))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("type") == "settings":
                output_sr = msg["data"].get("actual_output_sr", sample_rate)
                print(f"[{_ts()}] Server output sample rate: {output_sr} Hz")
                break

        # ---- Wait for pipeline warmup ----
        await ws.send(json.dumps({
            "type": "stream_start",
            "data": {"chunk_samples": chunk_samples, "extra_convert_size": extra_convert_size},
        }))
        print(f"[{_ts()}] Waiting for pipeline warmup...", end="", flush=True)
        while True:
            msg = json.loads(await ws.recv())
            mtype = msg.get("type")
            if mtype == "warmup_complete":
                print(" ready.")
                break
            elif mtype == "warmup_start":
                print(" warming up...", end="", flush=True)

        # ---- Set up audio I/O ----
        print(f"[{_ts()}] Opening audio devices...")
        import queue as _queue
        send_queue = _queue.Queue()
        spk_sr = output_sr if output_sr else sample_rate

        # Ring buffer for playback (2 seconds capacity)
        play_buf_size = spk_sr * 2
        play_buf = np.zeros(play_buf_size, dtype=np.float32)
        play_write_idx = 0
        play_read_idx = 0

        def play_buf_available():
            w, r = play_write_idx, play_read_idx
            return (w - r) if w >= r else (play_buf_size - r + w)

        def mic_callback(indata, frames, time_info, status):
            audio_int16 = (indata[:, 0] * 32768).clip(-32768, 32767).astype(np.int16)
            send_queue.put(audio_int16)

        def speaker_callback(outdata, frames, time_info, status):
            nonlocal play_read_idx
            avail = play_buf_available()
            n = min(frames, avail)
            if n > 0:
                r = play_read_idx
                if r + n <= play_buf_size:
                    outdata[:n, 0] = play_buf[r:r + n]
                else:
                    first = play_buf_size - r
                    outdata[:first, 0] = play_buf[r:]
                    outdata[first:n, 0] = play_buf[:n - first]
                play_read_idx = (r + n) % play_buf_size
            if n < frames:
                outdata[n:, 0] = 0

        print(f"[{_ts()}] Opening audio devices...")
        spk = sd.OutputStream(
            device=output_dev, samplerate=spk_sr, channels=1,
            dtype="float32", blocksize=0, callback=speaker_callback,
            latency="low",
        )
        spk.start()
        mic = sd.InputStream(
            device=input_dev, samplerate=sample_rate, channels=1,
            dtype="float32", blocksize=chunk_samples, callback=mic_callback,
        )
        mic.start()

        print(f"[{_ts()}] Streaming. Press Ctrl+C to stop.\n")

        # ---- Main streaming loops ----
        try:
            send_count = 0
            recv_count = 0

            async def send_loop():
                nonlocal send_count
                loop = asyncio.get_event_loop()
                while True:
                    chunk = await loop.run_in_executor(None, send_queue.get)
                    timestamp_bytes = struct.pack("<d", time.time() * 1000)
                    await ws.send(timestamp_bytes + chunk.tobytes())
                    send_count += 1

            async def recv_loop():
                nonlocal recv_count, play_write_idx
                while True:
                    data = await ws.recv()
                    if isinstance(data, str):
                        continue
                    if not isinstance(data, bytes):
                        continue

                    json_len = struct.unpack("<I", data[:4])[0]
                    meta = json.loads(data[4:4 + json_len])
                    audio = np.frombuffer(data[4 + json_len:], dtype=np.int16).astype(np.float32) / 32768.0
                    rtt = time.time() * 1000 - meta["timestamp"]

                    # Resample to speaker rate if needed
                    audio = _resample(audio, output_sr, spk_sr)

                    # Write into ring buffer
                    n = len(audio)
                    if n > 0:
                        w = play_write_idx
                        if w + n <= play_buf_size:
                            play_buf[w:w + n] = audio
                        else:
                            first = play_buf_size - w
                            play_buf[w:] = audio[:first]
                            play_buf[:n - first] = audio[first:]
                        play_write_idx = (w + n) % play_buf_size

                    recv_count += 1
                    buf_ms = play_buf_available() / spk_sr * 1000

                    # Periodic status
                    if recv_count % 20 == 0:
                        lat = meta.get("latency", {})
                        inf = lat.get("inference")
                        total = lat.get("total")
                        inf_str = f"{float(inf):.1f}" if inf is not None else "?"
                        total_str = f"{float(total):.1f}" if total is not None else "?"
                        print(
                            f"  [{_ts()}] [chunk {recv_count}] "
                            f"server={total_str}ms  model={inf_str}ms  "
                            f"rtt={rtt:.0f}ms  buf={buf_ms:.0f}ms",
                            flush=True,
                        )

            try:
                await asyncio.gather(send_loop(), recv_loop())
            except asyncio.CancelledError:
                pass
        finally:
            mic.stop()
            spk.stop()
            mic.close()
            spk.close()



def main():
    parser = argparse.ArgumentParser(
        description="Resemble voice conversion streaming client",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--server", default="http://localhost:8000",
                        help="Server URL (http or https)")
    parser.add_argument("--api-key", default="",
                        help="API key for authentication")
    parser.add_argument("--basic-user", default="",
                        help="HTTP basic auth username (if provided)")
    parser.add_argument("--basic-pass", default="",
                        help="HTTP basic auth password (if provided)")
    parser.add_argument("--voice", default="",
                        help="Voice model name (omit to choose interactively)")
    parser.add_argument("--sample-rate", type=int, default=48000,
                        help="Input audio sample rate in Hz")
    parser.add_argument("--chunk-samples", type=int, default=5760,
                        help="Chunk size in samples (default 5760 = 120ms at 48kHz)")
    parser.add_argument("--extra-convert-size", type=int, default=32784,
                        help="Extra context samples for conversion quality")
    args = parser.parse_args()

    input_dev = _pick_device("input")
    output_dev = _pick_device("output")

    basic_auth = (args.basic_user, args.basic_pass) if args.basic_user else None

    ticket = _get_ticket(args.server, args.api_key, basic_auth=basic_auth)
    print(f"[{_ts()}] Authenticated.")

    try:
        asyncio.run(stream(
            server=args.server,
            ticket=ticket,
            input_dev=input_dev,
            output_dev=output_dev,
            sample_rate=args.sample_rate,
            chunk_samples=args.chunk_samples,
            voice=args.voice,
            extra_convert_size=args.extra_convert_size,
            basic_auth=basic_auth,
        ))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
