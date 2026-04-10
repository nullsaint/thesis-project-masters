"""
Render Cloud Server - Pure HTTP Audio Relay
============================================
This runs on Render.com. It does THREE things only:
  1. Receives audio bursts from ESP32 via POST /audio
  2. Serves audio to the local bot via GET /relay
  3. Sends @everyone alerts via Discord Webhook (no bot token needed)

NO discord.py. NO gateway connection. NO token conflicts.
All Discord bot features (voice, commands) run in local_voice_bot.py.
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

# ==========================================
# CONFIGURATION
# ==========================================
ESP32_AUTH_KEY = os.getenv('ESP32_AUTH_KEY', 'esp32secret')
DISCORD_WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_URL', '')  # Set this in Render env vars
HTTP_PORT = int(os.getenv('PORT', '8080'))

# ==========================================
# SHARED AUDIO BUFFER
# ==========================================
class AudioBuffer:
    def __init__(self, max_size=512 * 1024):  # 512KB ~= 32 seconds at 8kHz
        self.buffer = collections.deque(maxlen=max_size)
        self.lock = threading.Lock()
        self.last_data_time = 0

        # VAD state
        self.last_notify_time = 0
        self.notify_cooldown = 15
        self.volume_threshold = 500
        self.voice_history = collections.deque(maxlen=150)
        self.required_voice_frames = 50

    def write(self, data: bytes):
        with self.lock:
            self.buffer.extend(data)
            self.last_data_time = time.time()

        # VAD analysis
        try:
            rms = audioop.rms(data, 2)
            self.voice_history.append(1 if rms > self.volume_threshold else 0)

            if sum(self.voice_history) >= self.required_voice_frames:
                now = time.time()
                if now - self.last_notify_time > self.notify_cooldown:
                    self.last_notify_time = now
                    self.voice_history.clear()
                    print(f"[VAD] Conversation detected! RMS={rms}. Sending webhook alert.")
                    asyncio.get_event_loop().create_task(send_webhook_alert())
        except Exception:
            pass

    def drain(self, max_bytes: int = 32000) -> bytes:
        """Drain up to max_bytes for the local bot to consume."""
        with self.lock:
            available = min(max_bytes, len(self.buffer))
            if available == 0:
                return b''
            return bytes([self.buffer.popleft() for _ in range(available)])

    def is_active(self) -> bool:
        return (time.time() - self.last_data_time) < 5

    def size(self) -> int:
        return len(self.buffer)

audio_buffer = AudioBuffer()

# ==========================================
# DISCORD WEBHOOK ALERT (no bot token needed)
# ==========================================
async def send_webhook_alert():
    if not DISCORD_WEBHOOK_URL:
        print("[VAD] No webhook URL set. Skipping alert.")
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
# HTTP ROUTES
# ==========================================
async def handle_health(request):
    esp = "STREAMING" if audio_buffer.is_active() else "WAITING"
    buf = audio_buffer.size()
    return web.Response(
        text=f"Cloud relay server alive.\nESP32: {esp}\nBuffer: {buf} bytes",
        status=200
    )

async def handle_audio(request):
    """Receives 1-second audio bursts from ESP32."""
    if request.headers.get('X-Auth-Key', '') != ESP32_AUTH_KEY:
        return web.Response(text="Unauthorized", status=401)
    try:
        data = await request.read()
        if data:
            audio_buffer.write(data)
            print(f"[ESP32] Burst received: {len(data)} bytes | Total buffer: {audio_buffer.size()} bytes")
        return web.Response(text="OK", status=200)
    except Exception as e:
        print(f"[ESP32] Error: {e}")
        return web.Response(text="Error", status=500)

async def handle_relay(request):
    """
    Local bot polls this endpoint to get buffered audio.
    Returns up to 32KB of audio, or empty body if nothing buffered.
    """
    if request.headers.get('X-Auth-Key', '') != ESP32_AUTH_KEY:
        return web.Response(text="Unauthorized", status=401)
    data = audio_buffer.drain(32000)
    if data:
        print(f"[RELAY] Serving {len(data)} bytes to local bot")
    return web.Response(
        body=data,
        content_type='application/octet-stream',
        status=200
    )

# ==========================================
# SERVER STARTUP
# ==========================================
async def create_app():
    app = web.Application()
    app.router.add_get('/', handle_health)
    app.router.add_get('/health', handle_health)
    app.router.add_post('/audio', handle_audio)
    app.router.add_post('/stream', handle_audio)  # backward compat alias
    app.router.add_get('/relay', handle_relay)
    return app

if __name__ == '__main__':
    print(f"[SERVER] Starting cloud relay server on port {HTTP_PORT}")
    print(f"[SERVER] ESP32 → POST /audio")
    print(f"[SERVER] Local bot → GET /relay")
    print(f"[SERVER] Webhook alerts: {'ENABLED' if DISCORD_WEBHOOK_URL else 'DISABLED (set DISCORD_WEBHOOK_URL)'}")
    web.run_app(create_app(), port=HTTP_PORT)
