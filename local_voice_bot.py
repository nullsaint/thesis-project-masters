"""
local_voice_bot.py
==================
Run this on your LOCAL MACHINE (laptop/PC).

It polls your Render cloud server for audio data, then plays it
in Discord voice. This bypasses the Discord voice block on cloud IPs.

Usage:
    pip install discord.py aiohttp python-dotenv PyNaCl
    python local_voice_bot.py

Works from ANY network — home, university, demo hall — without
port forwarding. The Render server is the middleman for audio.
"""

import discord
from discord.ext import commands
import asyncio
import audioop
import aiohttp
import os
import collections
import threading
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# CONFIGURATION
# ==========================================
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
NOTIFICATION_CHANNEL_ID = os.getenv('NOTIFICATION_CHANNEL_ID')
ESP32_AUTH_KEY = os.getenv('ESP32_AUTH_KEY', 'esp32secret')

# Your Render server URL
RENDER_RELAY_URL = "https://thesis-project-masters.onrender.com/relay"

# Poll Render every N seconds for audio chunks
POLL_INTERVAL = 0.9  # Just under 1 second to keep buffer flowing

# ==========================================
# LOCAL AUDIO BUFFER
# ==========================================
class LocalAudioBuffer:
    def __init__(self, max_size=512*1024):
        self.buffer = collections.deque(maxlen=max_size)
        self.lock = threading.Lock()

    def write(self, data: bytes):
        with self.lock:
            self.buffer.extend(data)

    def read(self, num_bytes: int) -> bytes:
        with self.lock:
            available = min(num_bytes, len(self.buffer))
            if available == 0:
                return bytes(num_bytes)  # silence
            data = bytes([self.buffer.popleft() for _ in range(available)])
            if len(data) < num_bytes:
                data += bytes(num_bytes - len(data))
            return data

    def size(self):
        return len(self.buffer)

local_buffer = LocalAudioBuffer()

# ==========================================
# DISCORD AUDIO SOURCE
# ==========================================
class RelayAudioSource(discord.AudioSource):
    """Reads from local buffer and resamples 8kHz → 48kHz for Discord."""
    def read(self):
        raw = local_buffer.read(320)  # 160 samples @ 8kHz = 20ms
        try:
            resampled, _ = audioop.ratecv(raw, 2, 1, 8000, 48000, None)
            return resampled
        except Exception:
            return bytes(3840)  # silence

    def cleanup(self):
        pass

# ==========================================
# RENDER POLLING TASK
# ==========================================
async def poll_render():
    """
    Continuously polls Render's /relay endpoint for audio chunks.
    Runs in background — completely independent of Discord voice.
    """
    print(f"[RELAY] Starting audio poll from: {RENDER_RELAY_URL}")
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(
                    RENDER_RELAY_URL,
                    headers={"X-Auth-Key": ESP32_AUTH_KEY},
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        if data:
                            local_buffer.write(data)
                            print(f"[RELAY] Got {len(data)} bytes | Buffer: {local_buffer.size()} bytes")
            except Exception as e:
                print(f"[RELAY] Poll error: {e}")
            await asyncio.sleep(POLL_INTERVAL)

# ==========================================
# BOT SETUP
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

_voice_client = None

@bot.event
async def on_ready():
    print(f"[BOT] Local relay bot online as {bot.user}")
    print(f"[BOT] Polling Render for audio every {POLL_INTERVAL}s")
    # Start polling Render for audio immediately
    bot.loop.create_task(poll_render())
    bot.loop.create_task(auto_join_watchdog())

async def auto_join_watchdog():
    """Joins voice automatically once audio is flowing from Render."""
    global _voice_client
    await asyncio.sleep(10)
    while True:
        if local_buffer.size() > 5000:  # ~0.3s of audio buffered — ESP32 is live
            if not _voice_client or not _voice_client.is_connected():
                print("[WATCHDOG] Audio buffered. Auto-joining voice...")
                await connect_voice()
            elif not _voice_client.is_playing():
                _voice_client.play(
                    RelayAudioSource(),
                    after=lambda e: print(f"[BOT] Playback ended: {e}") if e else None
                )
        await asyncio.sleep(10)

async def connect_voice():
    global _voice_client
    guild = next(iter(bot.guilds), None)
    if not guild or not guild.voice_channels:
        print("[BOT] No voice channel found.")
        return False

    channel = guild.voice_channels[0]
    try:
        if _voice_client:
            await _voice_client.disconnect(force=True)
            _voice_client = None
            await asyncio.sleep(1)

        print(f"[BOT] Connecting to {channel.name}...")
        _voice_client = await channel.connect(timeout=30.0, reconnect=True)

        # Wait for connection
        for _ in range(20):
            if _voice_client.is_connected():
                break
            await asyncio.sleep(0.25)

        if not _voice_client.is_connected():
            print("[BOT] Connection timed out.")
            return False

        _voice_client.play(
            RelayAudioSource(),
            after=lambda e: print(f"[BOT] Playback ended: {e}") if e else None
        )
        print(f"[BOT] ✅ Streaming audio in {channel.name}")
        return True

    except Exception as e:
        print(f"[BOT] Voice error: {type(e).__name__}: {e}")
        _voice_client = None
        return False

# ==========================================
# COMMANDS
# ==========================================
@bot.command()
async def listen(ctx):
    """Join voice and start relaying ESP32 audio."""
    await ctx.send("🎙️ Connecting...")
    success = await connect_voice()
    if success:
        buf = local_buffer.size()
        await ctx.send(f"✅ Connected! Buffer: {buf} bytes {'🟢' if buf > 1000 else '🟡 (waiting for ESP32)'}")
    else:
        await ctx.send("❌ Failed. Is the bot token correct? Check your terminal.")

@bot.command()
async def stop(ctx):
    """Leave voice channel."""
    global _voice_client
    if _voice_client:
        await _voice_client.disconnect(force=True)
        _voice_client = None
        await ctx.send("🛑 Disconnected.")
    else:
        await ctx.send("Not connected.")

@bot.command()
async def status(ctx):
    """Show relay status."""
    buf = local_buffer.size()
    vc_ok = bool(_voice_client and _voice_client.is_connected())
    playing = bool(_voice_client and _voice_client.is_playing()) if vc_ok else False

    embed = discord.Embed(title="📊 Local Relay Bot Status", color=0x00ff00 if vc_ok else 0xff9900)
    embed.add_field(name="Render Relay", value=f"📡 {RENDER_RELAY_URL}", inline=False)
    embed.add_field(name="Local Buffer", value=f"{buf} bytes {'🟢' if buf > 5000 else '🔴 (no ESP32 data)'}", inline=True)
    embed.add_field(name="Voice Channel", value="✅ Connected" if vc_ok else "❌ Not connected", inline=True)
    embed.add_field(name="Playing Audio", value="✅ Yes" if playing else "❌ No", inline=True)
    await ctx.send(embed=embed)

# ==========================================
# MAIN
# ==========================================
if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        print("[ERROR] DISCORD_BOT_TOKEN not set in .env!")
        print("Create a .env file with: DISCORD_BOT_TOKEN=your_token_here")
    else:
        print("[BOT] Starting LOCAL relay bot...")
        print(f"[BOT] Will poll: {RENDER_RELAY_URL}")
        bot.run(DISCORD_BOT_TOKEN)
