import discord
from discord.ext import commands, tasks
import asyncio
import os
import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from collections import deque
from datetime import datetime, timedelta
from aiohttp import web
from urllib.parse import urlparse

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# Spotify setup
sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
    client_id=os.getenv('SPOTIFY_CLIENT_ID'),
    client_secret=os.getenv('SPOTIFY_CLIENT_SECRET')
))

class MusicPlayer:
    def __init__(self):
        self.queue = deque()
        self.current = None
        self.loop = False
        self.last_activity = datetime.now()
        self.voice_client = None

music_players = {}

YTDL_OPTIONS = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': False,
    'nocheckcertificate': False,
    'ignoreerrors': True,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0'
}

FFMPEG_OPTIONS = {
    'before_options': '-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

def get_player(guild_id):
    if guild_id not in music_players:
        music_players[guild_id] = MusicPlayer()
    return music_players[guild_id]

def is_url(text: str) -> bool:
    try:
        parsed = urlparse(text)
        return parsed.scheme in ('http', 'https') and bool(parsed.netloc)
    except Exception:
        return False

async def search_youtube_first(query):
    q = f"ytsearch1:{query}"
    with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
        try:
            info = ydl.extract_info(q, download=False)
            if 'entries' in info and info['entries']:
                return info['entries'][0]
            return None
        except Exception as e:
            print(f"Error searching: {e}")
            return None

async def get_audio_source(url_or_query):
    target = url_or_query
    if not is_url(url_or_query):
        target = f"ytsearch1:{url_or_query}"

    with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
        try:
            info = ydl.extract_info(target, download=False)
            if 'entries' in info and info['entries']:
                info = info['entries'][0]

            stream_url = info.get('url')
            title = info.get('title', 'Unknown')
            webpage_url = info.get('webpage_url') or url_or_query

            if not stream_url:
                return None

            return {'url': stream_url, 'title': title, 'webpage_url': webpage_url}
        except Exception as e:
            print(f"Error getting audio: {e}")
            return None

def play_next(guild_id):
    player = get_player(guild_id)

    if player.loop and player.current:
        player.queue.appendleft(player.current)

    if not player.queue:
        player.current = None
        return

    player.current = player.queue.popleft()
    player.last_activity = datetime.now()

    try:
        source = discord.FFmpegPCMAudio(
            player.current['url'],
            executable='ffmpeg',
            **FFMPEG_OPTIONS
        )
        player.voice_client.play(source, after=lambda e: after_song(guild_id, e))
    except Exception as e:
        print(f"Error playing: {e}")
        play_next(guild_id)

def after_song(guild_id, error):
    if error:
        print(f"Player error: {error}")
    bot.loop.create_task(continue_playback(guild_id))

async def continue_playback(guild_id):
    await asyncio.sleep(0.5)
    play_next(guild_id)

@bot.event
async def on_ready():
    print(f'{bot.user} is ready!')
    check_inactive.start()

@tasks.loop(minutes=1)
async def check_inactive():
    for guild_id, player in list(music_players.items()):
        vc = player.voice_client
        if vc and vc.is_connected():
            idle_for = datetime.now() - player.last_activity
            if idle_for > timedelta(minutes=5) and not (vc.is_playing() or vc.is_paused()):
                await vc.disconnect()
                music_players.pop(guild_id, None)

@bot.command()
async def play(ctx, *, query):
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("You need to be in a voice channel!")
        return

    player = get_player(ctx.guild.id)

    if not player.voice_client or not player.voice_client.is_connected():
        player.voice_client = await ctx.author.voice.channel.connect()

    await ctx.send(f"🔍 Searching for: **{query}**")

    source = await get_audio_source(query)
    if source:
        player.queue.append(source)
        await ctx.send(f"✅ Added to queue: **{source['title']}**")
    else:
        await ctx.send("❌ Could not find a playable stream.")
        return

    player.last_activity = datetime.now()

    if player.voice_client and not (player.voice_client.is_playing() or player.voice_client.is_paused()):
        play_next(ctx.guild.id)

@bot.command()
async def pause(ctx):
    player = get_player(ctx.guild.id)
    if player.voice_client and player.voice_client.is_playing():
        player.voice_client.pause()
        await ctx.send("⏸️ Paused")
    else:
        await ctx.send("Nothing is playing!")

@bot.command()
async def resume(ctx):
    player = get_player(ctx.guild.id)
    if player.voice_client and player.voice_client.is_paused():
        player.voice_client.resume()
        await ctx.send("▶️ Resumed")
    else:
        await ctx.send("Nothing is paused!")

@bot.command()
async def skip(ctx):
    player = get_player(ctx.guild.id)
    if player.voice_client and player.voice_client.is_playing():
        player.voice_client.stop()
        await ctx.send("⏭️ Skipped")
    else:
        await ctx.send("Nothing is playing!")

@bot.command()
async def stop(ctx):
    player = get_player(ctx.guild.id)
    player.queue.clear()
    player.current = None
    if player.voice_client:
        player.voice_client.stop()
    await ctx.send("⏹️ Stopped and cleared queue")

@bot.command()
async def loop(ctx):
    player = get_player(ctx.guild.id)
    player.loop = not player.loop
    status = "enabled" if player.loop else "disabled"
    await ctx.send(f"🔁 Loop {status}")

@bot.command()
async def queue(ctx):
    player = get_player(ctx.guild.id)
    if not player.queue and not player.current:
        await ctx.send("Queue is empty!")
        return

    embed = discord.Embed(title="🎵 Music Queue", color=discord.Color.blue())

    if player.current:
        embed.add_field(name="Now Playing", value=f"**{player.current['title']}**", inline=False)

    if player.queue:
        queue_list = '\n'.join([f"{i+1}. {song['title']}" for i, song in enumerate(list(player.queue)[:10])])
        embed.add_field(name=f"Up Next ({len(player.queue)} songs)", value=queue_list, inline=False)

    if player.loop:
        embed.set_footer(text="🔁 Loop is enabled")

    await ctx.send(embed=embed)

@bot.command()
async def leave(ctx):
    player = get_player(ctx.guild.id)
    if player.voice_client:
        await player.voice_client.disconnect()
        player.queue.clear()
        player
