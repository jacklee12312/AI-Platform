from __future__ import annotations

import asyncio
import json
import os
import sys
import wave

import websockets


async def main() -> None:
    filename = sys.argv[1]
    url = os.getenv("GATEWAY_WS_URL", "ws://127.0.0.1:17700/v1/audio/transcriptions/stream")
    api_key = os.environ["HOUMO_API_KEY"]
    with wave.open(filename, "rb") as handle:
        assert handle.getframerate() == 16000
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        pcm = handle.readframes(handle.getnframes())

    async with websockets.connect(url, additional_headers={"Authorization": f"Bearer {api_key}"}) as socket:
        await socket.send(json.dumps({
            "type": "start",
            "model": "sensevoice",
            "sample_rate": 16000,
            "channels": 1,
            "format": "pcm_s16le",
            "language": "auto",
            "use_itn": True,
        }))
        print(await socket.recv())
        frame_bytes = 1600 * 2
        for offset in range(0, len(pcm), frame_bytes):
            await socket.send(pcm[offset:offset + frame_bytes])
        await socket.send(json.dumps({"type": "stop"}))
        async for message in socket:
            print(message)
            if json.loads(message).get("type") == "done":
                break


if __name__ == "__main__":
    asyncio.run(main())
