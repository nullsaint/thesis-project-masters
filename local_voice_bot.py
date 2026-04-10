"""
local_voice_bot.py
==================
Run this on your LOCAL MACHINE (laptop/PC) or Android via Termux.

Features:
  - Polls Render cloud server for ESP32 audio
  - Plays it live in Discord voice channel
  - SIMULTANEOUSLY saves raw audio as WAV files (new file every hour)
  - Works from any network — home, university, anywhere

Usage:
    pip install discord.py aiohttp python-dotenv PyNaCl
    python local_voice_bot.py

On Android (Termux), recordings save to /sdcard/recordings/
On Windows/Linux, recordings save to ./recordings/
"""

import discord
from discord.ext import commands
import asyncio
import audioop
import aiohttp
import os
import wave
import struct
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

# Your Render server URL
RENDER_RELAY_URL = "https://thesis-project-masters.onrender.com/relay"

# Poll Render every N seconds for audio chunks
POLL_INTERVAL = 0.9  # Just under 1 second to keep buffer flowing

# Recording settings
# On Android/Termux, change this to: Path('/sdcard/recordings')
RECORDINGS_DIR = Path('./recordings')
NEW_FILE_EVERY_MINUTES = 60  # Start a new WAV file every hour

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
# WAV RECORDER (runs in parallel)
# ==========================================
class WavRecorder:
    """
    Writes raw 8kHz 16-bit mono PCM to WAV files.
    Creates a new file every hour automatically.
    Thread-safe — can be written from the async polling loop.
    """
    def __init__(self, recordings_dir: Path):
        self.dir = recordings_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._wav = None
        self._file_start_time = None
        self._lock = threading.Lock()
        self._total_bytes = 0
        self._open_new_file()

    def _open_new_file(self):
        """Open a new WAV file with current timestamp as filename."""
        if self._wav:
            self._wav.close()
        now = datetime.datetime.now()
        filename = self.dir / f"recording_{now.strftime('%Y-%m-%d_%H-%M-%S')}.wav"
        self._wav = wave.open(str(filename), 'wb')
        self._wav.setnchannels(1)        # Mono
        self._wav.setsampwidth(2)        # 16-bit
        self._wav.setframerate(8000)     # 8kHz (raw ESP32 rate)
        self._file_start_time = now
        self._total_bytes = 0
        print(f"[REC] New recording: {filename.name}")

    def write(self, data: bytes):
        """Write audio data. Rotates file every hour automatically."""
        with self._lock:
            # Rotate file if time limit reached
            elapsed = (datetime.datetime.now() - self._file_start_time).total_seconds()
            if elapsed >= NEW_FILE_EVERY_MINUTES * 60:
                self._open_new_file()
            self._wav.writeframes(data)
            self._total_bytes += len(data)

    def get_stats(self):
        """Return recording stats for the status command."""
        with self._lock:
            files = sorted(self.dir.glob('*.wav'))
            total_size = sum(f.stat().st_size for f in files)
            return len(files), total_size

    def list_files(self):
        """Return list of WAV files."""
        with self._lock:
            return sorted(self.dir.glob('*.wav'))

    def close(self):
        with self._lock:
            if self._wav:
                self._wav.close()

recorder = WavRecorder(RECORDINGS_DIR)

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
    Polls Render's /relay endpoint for audio chunks.
    SIMULTANEOUSLY:
      1. Feeds the local audio buffer → Discord voice playback
      2. Writes to WAV file on disk → persistent recording
    """
    print(f"[RELAY] Starting audio poll from: {RENDER_RELAY_URL}")
    print(f"[REC]   Saving recordings to: {RECORDINGS_DIR.resolve()}")
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
                            # Path 1: feed Discord voice buffer
                            local_buffer.write(data)
                            # Path 2: save to WAV file (parallel, non-blocking)
                            await asyncio.get_event_loop().run_in_executor(
                                None, recorder.write, data
                            )
                            print(f"[RELAY] {len(data)}B received | Buffer: {local_buffer.size()}B")
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
    """Show relay and recording status."""
    buf = local_buffer.size()
    vc_ok = bool(_voice_client and _voice_client.is_connected())
    playing = bool(_voice_client and _voice_client.is_playing()) if vc_ok else False
    num_files, total_size = recorder.get_stats()
    size_mb = total_size / (1024 * 1024)

    embed = discord.Embed(title="📊 Local Relay Bot Status", color=0x00ff00 if vc_ok else 0xff9900)
    embed.add_field(name="Render Relay", value=f"📡 {RENDER_RELAY_URL}", inline=False)
    embed.add_field(name="Local Buffer", value=f"{buf} bytes {'🟢' if buf > 5000 else '🔴 (no ESP32 data)'}", inline=True)
    embed.add_field(name="Voice Channel", value="✅ Connected" if vc_ok else "❌ Not connected", inline=True)
    embed.add_field(name="Playing Audio", value="✅ Yes" if playing else "❌ No", inline=True)
    embed.add_field(name="Recordings", value=f"📁 {num_files} files | {size_mb:.1f} MB", inline=False)
    embed.add_field(name="Save Location", value=f"`{RECORDINGS_DIR.resolve()}`", inline=False)
    await ctx.send(embed=embed)

@bot.command()
async def recordings(ctx):
    """List saved WAV recordings."""
    files = recorder.list_files()
    if not files:
        await ctx.send("📁 No recordings yet. Start the ESP32 to begin recording.")
        return
    lines = []
    for f in files[-10:]:  # Show last 10
        size_kb = f.stat().st_size / 1024
        # Estimate duration: 8000 samples/s * 2 bytes = 16000 bytes/s
        duration_s = f.stat().st_size / 16000
        lines.append(f"`{f.name}` — {size_kb:.0f} KB, ~{duration_s/60:.1f} min")
    msg = f"📁 **Recordings** (last {len(lines)}):\n" + "\n".join(lines)
    msg += f"\n\n📂 Saved to: `{RECORDINGS_DIR.resolve()}`"
    await ctx.send(msg)

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
