import discord
from discord.ext import commands
import os
import audioop
import time
import collections
import asyncio
import threading
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# CONFIGURATION
# ==========================================
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
NOTIFICATION_CHANNEL_ID = os.getenv('NOTIFICATION_CHANNEL_ID')
ESP32_AUTH_KEY = os.getenv('ESP32_AUTH_KEY', 'esp32secret')
HTTP_PORT = int(os.getenv('PORT', '8080'))

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# ==========================================
# SHARED AUDIO BUFFER
# ==========================================
class AudioBuffer:
    def __init__(self, max_size=1024*1024):
        self.buffer = collections.deque(maxlen=max_size)
        self.lock = threading.Lock()
        self.last_data_time = 0

        # VAD state
        self.last_notify_time = 0
        self.notify_cooldown = 15       # seconds between alerts
        self.volume_threshold = 500     # RMS threshold
        self.voice_history = collections.deque(maxlen=150)  # 3s window
        self.required_voice_frames = 50 # ~1s of speech needed

    def write(self, data):
        with self.lock:
            self.buffer.extend(data)
            self.last_data_time = time.time()

        # VAD: run on every incoming chunk
        try:
            rms = audioop.rms(data, 2)
            self.voice_history.append(1 if rms > self.volume_threshold else 0)

            if sum(self.voice_history) >= self.required_voice_frames:
                current_time = time.time()
                if current_time - self.last_notify_time > self.notify_cooldown:
                    # Only alert if no humans are in the voice channel
                    humans_present = False
                    if bot.voice_clients:
                        vc = bot.voice_clients[0]
                        if vc.channel:
                            humans_present = any(not m.bot for m in vc.channel.members)

                    if not humans_present:
                        self.last_notify_time = current_time
                        self.voice_history.clear()
                        print(f"[VAD] Conversation detected! RMS={rms}")
                        channel = None
                        if NOTIFICATION_CHANNEL_ID and NOTIFICATION_CHANNEL_ID.isdigit():
                            channel = bot.get_channel(int(NOTIFICATION_CHANNEL_ID))
                        if channel:
                            bot.loop.create_task(channel.send(
                                "@everyone 🚨 **Conversation Detected by ESP32 Monitor!** Continuous talking identified!"
                            ))
        except Exception:
            pass

    def read(self, num_bytes):
        with self.lock:
            available = min(num_bytes, len(self.buffer))
            if available == 0:
                return bytes(num_bytes)
            data = bytes([self.buffer.popleft() for _ in range(available)])
            if len(data) < num_bytes:
                data += bytes(num_bytes - len(data))
            return data

    def is_active(self):
        return (time.time() - self.last_data_time) < 5

audio_buffer = AudioBuffer()

# ==========================================
# DISCORD AUDIO SOURCE
# ==========================================
class ESP32AudioSource(discord.AudioSource):
    def read(self):
        """
        Discord calls this every 20ms expecting 3840 bytes (48kHz 16-bit mono).
        We read 320 bytes (8kHz) and upsample 6x to 48kHz.
        """
        esp32_data = audio_buffer.read(320)
        try:
            resampled, _ = audioop.ratecv(esp32_data, 2, 1, 8000, 48000, None)
            return resampled
        except Exception:
            return bytes(3840)

    def cleanup(self):
        pass

# ==========================================
# HTTP SERVER (receives ESP32 audio)
# ==========================================
async def handle_health(request):
    status = "STREAMING" if audio_buffer.is_active() else "WAITING"
    return web.Response(text=f"Bot is alive. ESP32: {status}", status=200)

async def handle_audio(request):
    """Receives 1-second audio bursts from ESP32 via POST /audio"""
    if request.headers.get('X-Auth-Key', '') != ESP32_AUTH_KEY:
        return web.Response(text="Unauthorized", status=401)
    try:
        data = await request.read()
        if data:
            audio_buffer.write(data)
            print(f"[HTTP] Audio burst received: {len(data)} bytes")
        return web.Response(text="OK", status=200)
    except Exception as e:
        print(f"[HTTP] Error: {e}")
        return web.Response(text="Error", status=500)

async def start_http_server():
    app = web.Application()
    app.router.add_get('/', handle_health)
    app.router.add_get('/health', handle_health)
    app.router.add_post('/audio', handle_audio)
    app.router.add_post('/stream', handle_audio)  # fallback alias
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', HTTP_PORT)
    await site.start()
    print(f"[HTTP] Server started on port {HTTP_PORT}")

# ==========================================
# VOICE CONNECTION MANAGEMENT
# ==========================================
# The one and only voice connection state
_voice_client = None
_is_joining = False  # Simple flag to prevent concurrent joins

async def ensure_voice_connected():
    """
    Make sure the bot is in a voice channel and playing.
    If already connected and playing — do nothing.
    Only called when we are sure we need to connect.
    """
    global _voice_client, _is_joining

    # If already connected and playing, do nothing
    if _voice_client and _voice_client.is_connected() and _voice_client.is_playing():
        return

    # Prevent concurrent joins
    if _is_joining:
        return
    _is_joining = True

    try:
        # Find target guild and voice channel
        guild = None
        for g in bot.guilds:
            guild = g
            break
        if not guild:
            print("[BOT] No guild found!")
            return

        voice_channel = guild.voice_channels[0] if guild.voice_channels else None
        if not voice_channel:
            print("[BOT] No voice channels found!")
            return

        # Find notification text channel
        text_channel = None
        if NOTIFICATION_CHANNEL_ID and NOTIFICATION_CHANNEL_ID.isdigit():
            text_channel = bot.get_channel(int(NOTIFICATION_CHANNEL_ID))
        if not text_channel and guild.text_channels:
            text_channel = guild.text_channels[0]

        # If connected but not playing (glitch), stop and reconnect
        if _voice_client and _voice_client.is_connected():
            if not _voice_client.is_playing():
                source = ESP32AudioSource()
                _voice_client.play(source, after=lambda e: print(f"[BOT] Playback ended: {e}"))
                print("[BOT] Resumed playback on existing connection.")
                return

        # Fresh connect
        print(f"[BOT] Connecting to {voice_channel.name}...")
        _voice_client = await voice_channel.connect(timeout=30.0, reconnect=False)

        # Wait for connection to stabilize
        for _ in range(20):
            if _voice_client.is_connected():
                break
            await asyncio.sleep(0.25)

        # Start streaming
        source = ESP32AudioSource()
        _voice_client.play(source, after=lambda e: print(f"[BOT] Playback ended: {e}"))
        print(f"[BOT] Now streaming in {voice_channel.name}")

    except Exception as e:
        print(f"[BOT] Voice connect error: {type(e).__name__}: {e}")
        _voice_client = None
    finally:
        _is_joining = False

# ==========================================
# BOT EVENTS & BACKGROUND WATCHDOG
# ==========================================
@bot.event
async def on_ready():
    print(f"[BOT] Logged in as {bot.user}")
    print(f"[BOT] Guilds: {[g.name for g in bot.guilds]}")
    await start_http_server()
    print("[BOT] Ready. Watchdog starting in 10s...")
    bot.loop.create_task(voice_watchdog())

async def voice_watchdog():
    """
    The ONLY place reconnection happens.
    Checks every 10 seconds whether we need to join voice.
    Prevents the self-destructive reconnect loop.
    """
    await asyncio.sleep(10)
    while True:
        try:
            if audio_buffer.is_active():
                if not _voice_client or not _voice_client.is_connected() or not _voice_client.is_playing():
                    print("[WATCHDOG] Audio active but not in voice. Connecting...")
                    await ensure_voice_connected()
        except Exception as e:
            print(f"[WATCHDOG] Error: {e}")
        await asyncio.sleep(10)

# ==========================================
# COMMANDS
# ==========================================
@bot.command()
async def listen(ctx):
    """Force join the voice channel."""
    global _voice_client
    await ctx.send("🎙️ Forcing voice channel join...")
    try:
        # Disconnect cleanly if already connected
        if _voice_client:
            await _voice_client.disconnect(force=True)
            _voice_client = None
            await asyncio.sleep(2)
        await ensure_voice_connected()
        if _voice_client and _voice_client.is_connected():
            esp = "🟢 ACTIVE" if audio_buffer.is_active() else "🟡 WAITING for ESP32..."
            await ctx.send(f"✅ Connected! ESP32: {esp}")
        else:
            await ctx.send("❌ Failed to connect. Check Render logs.")
    except Exception as e:
        await ctx.send(f"❌ Error: `{type(e).__name__}: {e}`")

@bot.command()
async def stop(ctx):
    """Leave the voice channel."""
    global _voice_client
    if _voice_client:
        await _voice_client.disconnect(force=True)
        _voice_client = None
        await ctx.send("🛑 Disconnected.")
    else:
        await ctx.send("Not in a voice channel.")

@bot.command()
async def status(ctx):
    """Show current status."""
    esp_active = audio_buffer.is_active()
    buf_size = len(audio_buffer.buffer)
    vc_ok = _voice_client and _voice_client.is_connected()
    playing = _voice_client and _voice_client.is_playing() if vc_ok else False

    embed = discord.Embed(
        title="📊 ESP32 Voice Monitor Status",
        color=0x00ff00 if esp_active else 0xff9900
    )
    embed.add_field(name="ESP32 Stream", value="🟢 ACTIVE" if esp_active else "🔴 INACTIVE", inline=True)
    embed.add_field(name="Audio Buffer", value=f"{buf_size} bytes", inline=True)
    embed.add_field(name="Voice Channel", value="✅ Connected" if vc_ok else "❌ Not connected", inline=True)
    embed.add_field(name="Playing Audio", value="✅ Yes" if playing else "❌ No", inline=True)
    await ctx.send(embed=embed)

@bot.command()
async def reset(ctx):
    """Force full reset."""
    global _voice_client
    if _voice_client:
        await _voice_client.disconnect(force=True)
        _voice_client = None
    await ctx.send("🔄 Reset complete. Watchdog will reconnect in ~10 seconds once ESP32 sends audio.")

# ==========================================
# MAIN
# ==========================================
if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        print("[ERROR] DISCORD_BOT_TOKEN not set!")
    else:
        print("[BOT] Starting...")
        bot.run(DISCORD_BOT_TOKEN)
