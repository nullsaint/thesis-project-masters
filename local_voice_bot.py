"""
local_voice_bot.py - Local Discord Voice Bot
=============================================
Run this on your laptop (or Android via Termux).

How it works:
  1. Connects to Render via WebSocket (persistent, real-time)
  2. Render pushes audio the INSTANT ESP32 sends it (no polling!)
  3. Audio plays live in Discord voice channel
  4. Audio saved to WAV files simultaneously

Usage:
    pip install discord.py aiohttp python-dotenv PyNaCl
    python local_voice_bot.py

On Android (Termux): change RECORDINGS_DIR to Path('/sdcard/recordings')
"""

import discord
from discord.ext import commands
import asyncio
import audioop
import aiohttp
import os
import wave
import collections
import threading
import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# CONFIGURATION
# ==========================================
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
NOTIFICATION_CHANNEL_ID = os.getenv('NOTIFICATION_CHANNEL_ID')
ESP32_AUTH_KEY = os.getenv('ESP32_AUTH_KEY', 'esp32secret')
RENDER_WS_URL = "wss://thesis-project-masters.onrender.com/ws"

# Recordings: change to Path('/sdcard/recordings') on Android
RECORDINGS_DIR = Path('./recordings')
NEW_FILE_EVERY_MINUTES = 60

# ==========================================
# LOCAL AUDIO BUFFER
# ==========================================
class LocalBuffer:
    def __init__(self, max_size=512*1024):
        self.buf = collections.deque(maxlen=max_size)
        self.lock = threading.Lock()
        self.last_write = 0

    def write(self, data: bytes):
        with self.lock:
            self.buf.extend(data)
            self.last_write = datetime.datetime.now().timestamp()

    def read(self, n: int) -> bytes:
        with self.lock:
            avail = min(n, len(self.buf))
            if avail == 0:
                return bytes(n)
            out = bytes([self.buf.popleft() for _ in range(avail)])
            if len(out) < n:
                out += bytes(n - len(out))
            return out

    def size(self) -> int:
        return len(self.buf)

    def is_active(self) -> bool:
        return (datetime.datetime.now().timestamp() - self.last_write) < 5

local_buffer = LocalBuffer()

# ==========================================
# WAV RECORDER
# ==========================================
class WavRecorder:
    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self._wav = None
        self._start = None
        self._lock = threading.Lock()
        self._open_new()

    def _open_new(self):
        if self._wav:
            self._wav.close()
        now = datetime.datetime.now()
        path = self.dir / f"rec_{now.strftime('%Y-%m-%d_%H-%M-%S')}.wav"
        self._wav = wave.open(str(path), 'wb')
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(8000)
        self._start = now
        print(f"[REC] New file: {path.name}")

    def write(self, data: bytes):
        with self._lock:
            elapsed = (datetime.datetime.now() - self._start).total_seconds()
            if elapsed >= NEW_FILE_EVERY_MINUTES * 60:
                self._open_new()
            self._wav.writeframes(data)

    def stats(self):
        files = sorted(self.dir.glob('*.wav'))
        total = sum(f.stat().st_size for f in files)
        return len(files), total / (1024*1024)

    def list_recent(self, n=10):
        return sorted(self.dir.glob('*.wav'))[-n:]

recorder = WavRecorder(RECORDINGS_DIR)

# ==========================================
# DISCORD AUDIO SOURCE
# ==========================================
class ESP32AudioSource(discord.AudioSource):
    """Reads 8kHz PCM from buffer, resamples to 48kHz for Discord."""
    def read(self):
        raw = local_buffer.read(320)
        try:
            out, _ = audioop.ratecv(raw, 2, 1, 8000, 48000, None)
            return out
        except Exception:
            return bytes(3840)

    def cleanup(self):
        pass

# ==========================================
# BOT SETUP
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

_vc = None          # Active voice client
_joining = False    # Guard against concurrent joins

# ==========================================
# WEBSOCKET CONNECTION TO RENDER
# ==========================================
async def render_ws_loop():
    """
    Maintains a persistent WebSocket connection to Render.
    Render pushes audio the instant ESP32 sends it.
    Auto-reconnects forever.
    """
    global _vc
    print(f"[WS] Connecting to {RENDER_WS_URL}")
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(
                    RENDER_WS_URL,
                    headers={'X-Auth-Key': ESP32_AUTH_KEY},
                    heartbeat=20,
                    timeout=aiohttp.ClientTimeout(total=None)  # No timeout — stays open
                ) as ws:
                    print("[WS] ✅ Connected to Render! Audio will stream in real-time.")
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.BINARY:
                            data = msg.data
                            # Feed Discord voice buffer
                            local_buffer.write(data)
                            # Save to WAV (non-blocking)
                            try:
                                loop = asyncio.get_running_loop()
                                await loop.run_in_executor(None, recorder.write, data)
                            except Exception:
                                pass
                            # Auto-join voice if we have audio and aren't connected
                            if not _vc or not _vc.is_connected():
                                asyncio.get_running_loop().create_task(auto_join())

                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            print(f"[WS] Connection closed: {msg.type}")
                            break

        except Exception as e:
            print(f"[WS] Disconnected ({type(e).__name__}). Reconnecting in 5s...")
        await asyncio.sleep(5)

async def auto_join():
    """Join voice automatically when audio starts flowing."""
    global _vc, _joining
    if _joining or (_vc and _vc.is_connected() and _vc.is_playing()):
        return
    _joining = True
    try:
        await connect_voice()
    finally:
        _joining = False

async def connect_voice() -> bool:
    global _vc
    guild = next(iter(bot.guilds), None)
    if not guild or not guild.voice_channels:
        return False
    channel = guild.voice_channels[0]

    try:
        # Clean up old connection
        if _vc:
            await _vc.disconnect(force=True)
            _vc = None
            await asyncio.sleep(1)

        print(f"[BOT] Connecting to {channel.name}...")
        _vc = await channel.connect(timeout=30.0, reconnect=True)

        # Wait for UDP handshake
        for _ in range(20):
            if _vc.is_connected():
                break
            await asyncio.sleep(0.25)

        if not _vc.is_connected():
            print("[BOT] Timed out waiting for voice connection.")
            return False

        _vc.play(
            ESP32AudioSource(),
            after=lambda e: print(f"[BOT] Stream ended: {e}") if e else None
        )
        print(f"[BOT] ✅ Streaming in {channel.name}")
        return True

    except Exception as e:
        print(f"[BOT] Voice error: {type(e).__name__}: {e}")
        _vc = None
        return False

# ==========================================
# BOT EVENTS
# ==========================================
@bot.event
async def on_ready():
    print(f"[BOT] Online as {bot.user}")
    print(f"[BOT] Connecting to Render WebSocket...")
    bot.loop.create_task(render_ws_loop())

# ==========================================
# COMMANDS
# ==========================================
@bot.command()
async def listen(ctx):
    """Join the voice channel."""
    global _vc
    await ctx.send("🎙️ Connecting to voice...")
    if _vc:
        await _vc.disconnect(force=True)
        _vc = None
        await asyncio.sleep(1)
    ok = await connect_voice()
    if ok:
        active = "🟢 Audio flowing!" if local_buffer.is_active() else "🟡 Waiting for ESP32..."
        await ctx.send(f"✅ Connected! {active}")
    else:
        await ctx.send("❌ Failed. Check terminal for error details.")

@bot.command()
async def stop(ctx):
    """Leave the voice channel."""
    global _vc
    if _vc:
        await _vc.disconnect(force=True)
        _vc = None
        await ctx.send("🛑 Left voice channel.")
    else:
        await ctx.send("Not in a voice channel.")

@bot.command()
async def status(ctx):
    """Show full system status."""
    n_files, size_mb = recorder.stats()
    buf = local_buffer.size()
    vc_ok = bool(_vc and _vc.is_connected())
    playing = bool(_vc and _vc.is_playing()) if vc_ok else False
    ws_active = local_buffer.is_active()

    embed = discord.Embed(
        title="📊 ESP32 Surveillance Status",
        color=0x00ff00 if (vc_ok and ws_active) else 0xff9900
    )
    embed.add_field(name="Render WebSocket", value="🟢 Audio flowing" if ws_active else "🔴 No data (ESP32 off?)", inline=False)
    embed.add_field(name="Local Buffer", value=f"{buf} bytes", inline=True)
    embed.add_field(name="Voice Channel", value="✅ Connected" if vc_ok else "❌ Not connected", inline=True)
    embed.add_field(name="Playing Audio", value="✅ Yes" if playing else "❌ No", inline=True)
    embed.add_field(name="Recordings", value=f"📁 {n_files} files | {size_mb:.1f} MB", inline=True)
    embed.add_field(name="Save Path", value=f"`{RECORDINGS_DIR.resolve()}`", inline=False)
    await ctx.send(embed=embed)

@bot.command()
async def recordings(ctx):
    """List recent WAV recordings."""
    files = recorder.list_recent()
    if not files:
        await ctx.send("📁 No recordings yet.")
        return
    lines = []
    for f in files:
        kb = f.stat().st_size / 1024
        mins = f.stat().st_size / 16000 / 60
        lines.append(f"`{f.name}` — {kb:.0f} KB (~{mins:.1f} min)")
    await ctx.send("📁 **Recent Recordings:**\n" + "\n".join(lines))

@bot.command()
async def reset(ctx):
    """Force disconnect and let watchdog reconnect."""
    global _vc
    if _vc:
        await _vc.disconnect(force=True)
        _vc = None
    await ctx.send("🔄 Reset. Will auto-reconnect when Render sends audio.")

# ==========================================
# MAIN
# ==========================================
if __name__ == '__main__':
    if not DISCORD_BOT_TOKEN:
        print("[ERROR] DISCORD_BOT_TOKEN not in .env!")
    else:
        print("[BOT] Starting local voice bot...")
        bot.run(DISCORD_BOT_TOKEN)
