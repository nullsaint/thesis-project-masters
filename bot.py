"""
Render Cloud Server - WebSocket Audio Relay
============================================
Runs on Render.com. Receives audio from ESP32 and pushes it
in real-time to the local bot via WebSocket (no polling needed).
"""

import os
import time
import audioop
import collections
import threading
import asyncio
import aiohttp
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

ESP32_AUTH_KEY = os.getenv('ESP32_AUTH_KEY', 'esp32secret')
DISCORD_WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_URL', '')
HTTP_PORT = int(os.getenv('PORT', '8080'))

# ==========================================
# AUDIO BUFFER + VAD
# ==========================================
class AudioBuffer:
    def __init__(self):
        self.lock = threading.Lock()
        self.last_data_time = 0
        self.last_notify_time = 0
        self.notify_cooldown = 15
        self.volume_threshold = 500
        self.voice_history = collections.deque(maxlen=150)
        self.required_voice_frames = 50

    def analyze_vad(self, data: bytes):
        try:
            rms = audioop.rms(data, 2)
            self.voice_history.append(1 if rms > self.volume_threshold else 0)
            if sum(self.voice_history) >= self.required_voice_frames:
                now = time.time()
                if now - self.last_notify_time > self.notify_cooldown:
                    self.last_notify_time = now
                    self.voice_history.clear()
                    print(f"[VAD] Conversation detected! RMS={rms}")
                    asyncio.get_event_loop().create_task(send_webhook_alert())
        except Exception:
            pass

    def mark_received(self):
        self.last_data_time = time.time()

    def is_active(self):
        return (time.time() - self.last_data_time) < 5

audio_buffer = AudioBuffer()

# ==========================================
# WEBSOCKET RELAY CLIENTS
# ==========================================
ws_clients: set = set()

async def broadcast(data: bytes):
    """Push audio to all connected local bots instantly."""
    dead = set()
    for ws in list(ws_clients):
        try:
            await ws.send_bytes(data)
        except Exception:
            dead.add(ws)
    ws_clients.difference_update(dead)

# ==========================================
# DISCORD WEBHOOK ALERT
# ==========================================
async def send_webhook_alert():
    if not DISCORD_WEBHOOK_URL:
        print("[VAD] DISCORD_WEBHOOK_URL not set. Skipping alert.")
        return
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(DISCORD_WEBHOOK_URL, json={
                "content": "@everyone 🚨 **Conversation Detected by ESP32 Monitor!** Continuous talking identified!"
            })
            print("[VAD] Webhook alert sent!")
    except Exception as e:
        print(f"[VAD] Webhook error: {e}")

# ==========================================
# HTTP + WEBSOCKET ROUTES
# ==========================================
async def handle_health(request):
    clients = len(ws_clients)
    esp = "STREAMING" if audio_buffer.is_active() else "WAITING"
    return web.Response(
        text=f"OK | ESP32: {esp} | Relay clients: {clients}",
        status=200
    )

async def handle_audio(request):
    """Receives 1-second audio bursts from ESP32."""
    if request.headers.get('X-Auth-Key', '') != ESP32_AUTH_KEY:
        return web.Response(text="Unauthorized", status=401)
    try:
        data = await request.read()
        if data:
            audio_buffer.mark_received()
            audio_buffer.analyze_vad(data)
            await broadcast(data)  # Push to local bot instantly
            clients = len(ws_clients)
            print(f"[ESP32] {len(data)} bytes | Relay clients: {clients}")
        return web.Response(text="OK", status=200)
    except Exception as e:
        print(f"[ESP32] Error: {e}")
        return web.Response(text="Error", status=500)

async def handle_ws(request):
    """
    WebSocket endpoint for the local bot.
    Local bot connects here once, then receives audio pushed in real-time.
    """
    auth = request.headers.get('X-Auth-Key', '')
    if auth != ESP32_AUTH_KEY:
        return web.Response(text="Unauthorized", status=401)

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    ws_clients.add(ws)
    print(f"[WS] Local bot connected! Total clients: {len(ws_clients)}")

    try:
        async for msg in ws:
            pass  # Keep-alive — we only send, not receive
    except Exception as e:
        print(f"[WS] Client error: {e}")
    finally:
        ws_clients.discard(ws)
        print(f"[WS] Local bot disconnected. Clients: {len(ws_clients)}")

    return ws

async def create_app():
    app = web.Application()
    app.router.add_get('/', handle_health)
    app.router.add_get('/health', handle_health)
    app.router.add_post('/audio', handle_audio)
    app.router.add_post('/stream', handle_audio)
    app.router.add_get('/ws', handle_ws)       # WebSocket for local bot
    app.router.add_get('/relay', handle_health) # Keep old path alive (returns health now)
    return app

if __name__ == '__main__':
    print(f"[SERVER] Cloud relay starting on port {HTTP_PORT}")
    print(f"[SERVER] ESP32  → POST /audio")
    print(f"[SERVER] Local bot → WS  /ws")
    web.run_app(create_app(), port=HTTP_PORT)
