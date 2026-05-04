"""Quick smoke test: connect to local bridge and print any received events.

Run on pi-main alongside the bridge:
  /home/mine/Documents/fish_ai_realtime/.venv/bin/python _smoke_test.py
"""
import asyncio
import json
import websockets


async def main():
    async with websockets.connect("ws://localhost:8765/") as ws:
        print("[smoke] connected")
        try:
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=30)
                print(f"[smoke] {msg}", flush=True)
        except asyncio.TimeoutError:
            print("[smoke] no messages in 30s, exiting", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
