import discord
from discord.ext import commands, tasks
import os
import traceback
import audioop
import time
import collections
import asyncio
import threading
import io
from aiohttp import web
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# ==========================================
# CONFIGURATION
# ==========================================
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
NOTIFICATION_CHANNEL_ID = os.getenv('NOTIFICATION_CHANNEL_ID')
ESP32_AUTH_KEY = os.getenv('ESP32_AUTH_KEY', 'esp32secret')

# Port for the HTTP server that receives ESP32 audio
HTTP_PORT = int(os.getenv('PORT', '8080'))

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# ==========================================
# SHARED AUDIO BUFFER (ESP32 -> Discord)
# ==========================================
# Thread-safe circular buffer that the ESP32 pushes into
# and the Discord AudioSource reads from
class AudioBuffer:
    def __init__(self, max_size=1024*1024):  # 1MB buffer (~65 seconds of 8kHz 16-bit mono)
        self.buffer = collections.deque(maxlen=max_size)
        self.lock = threading.Lock()
        self.connected = False
        self.last_data_time = 0
        
        # VAD State (Surveillance Window)
        self.last_notify_time = 0
        self.notify_cooldown = 15
        self.volume_threshold = 500
        self.voice_history = collections.deque(maxlen=150)
        self.required_voice_frames = 50

    def write(self, data, target_channel_getter, voice_client_getter):
        with self.lock:
            self.buffer.extend(data)
            self.last_data_time = time.time()
            self.connected = True
            
        # LAZY JOIN: If we have data but aren't in voice, join now!
        voice_client = voice_client_getter()
        if not voice_client:
            # We use a task to avoid blocking the HTTP receiver
            bot.loop.create_task(self.auto_join_on_data())

        # 24/7 VAD Analysis...
        try:
            rms = audioop.rms(data, 2)
            self.voice_history.append(1 if rms > self.volume_threshold else 0)
            
            if sum(self.voice_history) >= self.required_voice_frames:
                current_time = time.time()
                if current_time - self.last_notify_time > self.notify_cooldown:
                    humans_present = False
                    if voice_client and voice_client.channel:
                        humans_present = any(not m.bot for m in voice_client.channel.members)
                    
                    if not humans_present:
                        print(f"[VAD] Conversation detected! RMS: {rms}")
                        self.last_notify_time = current_time
                        self.voice_history.clear()
                        channel = target_channel_getter()
                        if channel:
                            bot.loop.create_task(channel.send(
                                "@everyone 🚨 **Conversation Detected by ESP32 Monitor!** Continuous talking identified!"
                            ))
        except:
            pass

    async def auto_join_on_data(self):
        """Automatically join the first voice channel when data arrives."""
        async with connection_lock:
            if bot.voice_clients: return # Already joined
            for guild in bot.guilds:
                if guild.voice_channels:
                    target = guild.voice_channels[0]
                    text_out = guild.text_channels[0]
                    print(f"[BOT] Audio data detected! Auto-joining {target.name}...")
                    try:
                        await join_and_play(text_out, target)
                    except Exception as e:
                        print(f"[BOT] Lazy join failed: {e}")
                    return

    def read(self, num_bytes):
        with self.lock:
            available = min(num_bytes, len(self.buffer))
            if available == 0:
                # Return silence (zeros) if no data available
                return bytes(num_bytes)
            data = bytes([self.buffer.popleft() for _ in range(available)])
            # Pad with silence if we didn't have enough
            if len(data) < num_bytes:
                data += bytes(num_bytes - len(data))
            return data

    def is_active(self):
        """Check if ESP32 has sent data in the last 5 seconds"""
        return self.connected and (time.time() - self.last_data_time < 5)

    def clear(self):
        with self.lock:
            self.buffer.clear()

audio_buffer = AudioBuffer()

# ==========================================
# AIOHTTP WEB SERVER (Receives ESP32 Audio)
# ==========================================
app_runner = None

async def handle_health(request):
    """Health check endpoint for Railway / uptime monitors"""
    esp_status = "STREAMING" if audio_buffer.is_active() else "WAITING"
    return web.Response(text=f"Bot is alive. ESP32: {esp_status}", status=200)

async def handle_audio_stream(request):
    """
    Receives raw PCM audio pushed from the ESP32.
    ESP32 sends POST requests with raw audio bytes.
    """
    auth = request.headers.get('X-Auth-Key', '')
    if auth != ESP32_AUTH_KEY:
        return web.Response(text="Unauthorized", status=401)

    try:
        data = await request.read()
        if data:
            audio_buffer.write(data)
        return web.Response(text="OK", status=200)
    except Exception as e:
        print(f"Error receiving audio: {e}")
        return web.Response(text="Error", status=500)

async def handle_audio_stream_chunked(request):
    """
    Receives a long-lived chunked stream from ESP32.
    ESP32 keeps the connection open and pushes audio continuously.
    """
    auth = request.headers.get('X-Auth-Key', '')
    if auth != ESP32_AUTH_KEY:
        return web.Response(text="Unauthorized", status=401)

    print("[HTTP] ESP32 connected for streaming!")
    
    # Helper functions to get current channels for the VAD
    def get_target_channel():
        if NOTIFICATION_CHANNEL_ID and NOTIFICATION_CHANNEL_ID.isdigit():
            return bot.get_channel(int(NOTIFICATION_CHANNEL_ID))
        return None

    def get_voice_client():
        # This is a bit simplified; in a real bot we'd want to track which guild the ESP32 belongs to
        # But for your thesis (1 bot, 1 server), this works!
        if bot.voice_clients:
            return bot.voice_clients[0]
        return None

    try:
        async for chunk in request.content.iter_any():
            if chunk:
                audio_buffer.write(chunk, get_target_channel, get_voice_client)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"[HTTP] ESP32 stream error: {e}")
    finally:
        print("[HTTP] ESP32 stream disconnected.")

    return web.Response(text="Stream ended", status=200)

async def start_http_server():
    """Start the HTTP server that receives audio from ESP32"""
    app = web.Application()
    app.router.add_get('/', handle_health)
    app.router.add_get('/health', handle_health)
    app.router.add_post('/audio', handle_audio_stream)
    app.router.add_post('/stream', handle_audio_stream_chunked)

    global app_runner
    app_runner = web.AppRunner(app)
    await app_runner.setup()
    site = web.TCPSite(app_runner, '0.0.0.0', HTTP_PORT)
    await site.start()
    print(f"[HTTP] Audio receiver server started on port {HTTP_PORT}")

# ==========================================
# DISCORD AUDIO SOURCE (Reads from buffer)
# ==========================================
class ESP32AudioSource(discord.AudioSource):
    """
    Reads PCM audio from the shared buffer and feeds it to Discord.
    Handles resampling from ESP32's 8kHz to Discord's required 48kHz.
    Also performs Voice Activity Detection.
    """
    def __init__(self, text_channel, voice_client):
        self.text_channel = text_channel
        self.voice_client = voice_client
        # Cooldown and threshold settings moved to class level or init
        self.last_notify_time = 0
        self.notify_cooldown = 15 
        self.volume_threshold = 500 
        self.voice_history = collections.deque(maxlen=150)
        self.required_voice_frames = 50 

    def read(self):
        """
        Discord calls this every 20ms, expecting 3840 bytes of 48kHz 16-bit mono PCM.
        Our ESP32 sends 8kHz, so we read 320 bytes (160 samples) and upsample 6x to 48kHz.
        """
        # 160 samples at 8kHz = 20ms of audio = 320 bytes
        esp32_data = audio_buffer.read(320)

        # Resample: 8kHz 16-bit mono -> 48kHz 16-bit mono (multiply rate by 6)
        try:
            resampled, _ = audioop.ratecv(esp32_data, 2, 1, 8000, 48000, None)
            return resampled
        except Exception:
            # Return silence if resampling fails
            return bytes(3840)

    def cleanup(self):
        pass

# ==========================================
# BOT EVENTS & COMMANDS
# ==========================================
# Global lock to prevent multiple simultaneous connection attempts
connection_lock = asyncio.Lock()

@bot.event
async def on_ready():
    print(f'[BOT] Logged in as {bot.user}')
    print(f'[BOT] Commands: !listen, !stop, !status, !reset')

    # ZOMBIE KILLER: Force disconnect any hanging sessions on startup
    for vc in bot.voice_clients:
        try:
            print(f"[BOT] Clearing zombie session in {vc.guild.name}...")
            await vc.disconnect(force=True)
        except:
            pass
    
    # Start the HTTP server for receiving ESP32 audio
    await start_http_server()
    
    # AUTO-JOIN: Try to join the first available voice channel on startup
    # This reduces the need for user commands and prevents race conditions
    await asyncio.sleep(5) # Give it a moment to stabilize
    for guild in bot.guilds:
        if guild.voice_channels:
            target = guild.voice_channels[0]
            print(f"[BOT] Attempting Auto-Join to {target.name}...")
            # We use a fake context or just call the logic
            try:
                # Find a text channel for logs
                text_channel = guild.text_channels[0]
                await join_and_play(text_channel, target)
            except Exception as e:
                print(f"[BOT] Auto-join failed: {e}")

async def join_and_play(text_channel, voice_channel):
    """Core logic for joining and starting the stream with total stability."""
    async with connection_lock:
        try:
            # 1. Force cleanup
            existing_vc = discord.utils.get(bot.voice_clients, guild=voice_channel.guild)
            if existing_vc:
                await existing_vc.disconnect(force=True)
                await asyncio.sleep(2)

            # 2. Connect
            print(f"[BOT] Connecting to {voice_channel.name}...")
            vc = await voice_channel.connect(timeout=30.0, reconnect=True)
            
            # 3. Wait for the audio handshake to stabilize
            print("[BOT] Waiting for audio handshake...")
            for i in range(10):
                if vc.is_connected():
                    break
                await asyncio.sleep(0.5)

            # 4. Start streaming
            audio_source = ESP32AudioSource(text_channel, vc)
            if not vc.is_playing():
                vc.play(audio_source, after=lambda e: print(f'[BOT] Player error: {e}') if e else None)
                print(f"[BOT] Stream started in {voice_channel.name}")
                return True
        except Exception as e:
            print(f"[BOT] join_and_play error: {type(e).__name__}: {e}")
            raise e
    return False

@bot.command()
async def listen(ctx):
    """Join a voice channel and start monitoring ESP32 audio."""
    if connection_lock.locked():
        await ctx.send("⏳ I am already connecting! Please wait 10 seconds.")
        return

    if ctx.author.voice:
        channel = ctx.author.voice.channel
    else:
        if ctx.guild.voice_channels:
            channel = ctx.guild.voice_channels[0]
        else:
            await ctx.send("❌ No voice channels found!")
            return

    await ctx.send(f"📡 Attempting to join **{channel.name}** and start 24/7 surveillance...")
    try:
        success = await join_and_play(ctx.channel, channel)
        if success:
            esp_status = "🟢 CONNECTED" if audio_buffer.is_active() else "🟡 WAITING for ESP32 data..."
            await ctx.send(f"✅ **Surveillance ACTIVE** in {channel.name}\nESP32: {esp_status}")
    except Exception as e:
        await ctx.send(f"❌ Connection failed: `{type(e).__name__}: {e}`")

@bot.command()
async def reset(ctx):
    """Force a total reset of all voice connections."""
    for vc in bot.voice_clients:
        await vc.disconnect(force=True)
    await ctx.send("🔄 All voice connections forced to close. The bot will auto-join if audio is pushed, or you can try `!listen` in 5 seconds.")

@bot.command()
async def stop(ctx):
    """Stop listening and leave the voice channel."""
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("🛑 Stopped listening and left the channel.")
    else:
        await ctx.send("I'm not in a voice channel.")

@bot.command()
async def status(ctx):
    """Check the current status of the ESP32 connection."""
    esp_active = audio_buffer.is_active()
    buf_size = len(audio_buffer.buffer)

    vc_status = "Connected" if ctx.voice_client and ctx.voice_client.is_connected() else "Not connected"
    playing = "Yes" if ctx.voice_client and ctx.voice_client.is_playing() else "No"

    embed = discord.Embed(title="📊 ESP32 Voice Monitor Status", color=0x00ff00 if esp_active else 0xff9900)
    embed.add_field(name="ESP32 Stream", value="🟢 ACTIVE" if esp_active else "🔴 INACTIVE", inline=True)
    embed.add_field(name="Audio Buffer", value=f"{buf_size} bytes", inline=True)
    embed.add_field(name="Voice Channel", value=vc_status, inline=True)
    embed.add_field(name="Playing Audio", value=playing, inline=True)
    await ctx.send(embed=embed)

# ==========================================
# MAIN ENTRY POINT
# ==========================================
if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN or DISCORD_BOT_TOKEN == "paste_your_bot_token_here":
        print("[ERROR] Please set your Discord Bot Token in .env or environment variables!")
    else:
        print("[BOT] Starting Discord Bot with HTTP Audio Receiver...")
        bot.run(DISCORD_BOT_TOKEN)
