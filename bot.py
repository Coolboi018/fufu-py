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

# Music queue and state management
class MusicPlayer:
    def __init__(self):
        self.queue = deque()
        self.current = None
        self.loop = False
        self.last_activity = datetime.now()
        self.voice_client = None

music_players = {}

# yt-dlp: get direct audio URLs suitable for FFmpeg
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
    # IMPORTANT: do NOT set extract_flat; we want direct URLs
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

def extract_spotify_info(url):
    """Extract track/playlist/album info from Spotify URL"""
    if 'track' in url:
        track_id = url.split('track/')[1].split('?')[0]
        track = sp.track(track_id)
        return {
            'type': 'track',
            'name': f"{track['artists'][0]['name']} - {track['name']}",
            'url': url
        }
    elif 'playlist' in url:
        playlist_id = url.split('playlist/')[1].split('?')[0]
        playlist = sp.playlist(playlist_id)
        tracks = []
        for item in playlist['tracks']['items']:
            t = item['track']
            if t:  # guard against nulls
                tracks.append(f"{t['artists'][0]['name']} - {t['name']}")
        return {
            'type': 'playlist',
            'tracks': tracks,
            'name': playlist['name']
        }
    elif 'album' in url:
        album_id = url.split('album/')[1].split('?')[0]
        album = sp.album(album_id)
        tracks = []
        for t in album['tracks']['items']:
            tracks.append(f"{t['artists'][0]['name']} - {t['name']}")
        return {
            'type': 'album',
            'tracks': tracks,
            'name': album['name']
        }
    return None

async def search_youtube_first(query):
    """Return first full entry (not flat) from ytsearch1"""
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
    """Get a single playable audio source dict with url/title/webpage_url"""
    target = url_or_query
    if not is_url(url_or_query):
        target = f"ytsearch1:{url_or_query}"

    with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
        try:
            info = ydl.extract_info(target, download=False)
            # For playlists, choose first item
            if 'entries' in info and info['entries']:
                info = info['entries'][0]

            # Ensure we have a direct media URL
            stream_url = info.get('url')
            title = info.get('title', 'Unknown')
            webpage_url = info.get('webpage_url') or info.get('original_url') or url_or_query

            if not stream_url:
                print("No stream URL found in yt-dlp info")
                return None

            return {'url': stream_url, 'title': title, 'webpage_url': webpage_url}
        except Exception as e:
            print(f"Error getting audio: {e}")
            return None

def play_next(guild_id):
    """Play next song in queue"""
    player = get_player(guild_id)

    # If looping, re-queue the current track at front
    if player.loop and player.current:
        player.queue.appendleft(player.current)

    if not player.queue:
        player.current = None
        # Do not disconnect immediately; inactivity task will handle it
        return

    player.current = player.queue.popleft()
    player.last_activity = datetime.now()

    try:
        # Explicit ffmpeg executable; improves reliability in containers
        source = discord.FFmpegPCMAudio(
            player.current['url'],
            executable='ffmpeg',
            **FFMPEG_OPTIONS
        )
        player.voice_client.play(source, after=lambda e: after_song(guild_id, e))
    except Exception as e:
        print(f"Error playing: {e}")
        # Try the next item rather than looping forever on a bad source
        play_next(guild_id)

def after_song(guild_id, error):
    """Called after a song finishes"""
    if error:
        print(f"Player error: {error}")
    # Schedule on event loop safely
    bot.loop.create_task(continue_playback(guild_id))

async def continue_playback(guild_id):
    """Continue to next song"""
    await asyncio.sleep(0.5)
    play_next(guild_id)

@bot.event
async def on_ready():
    print(f'{bot.user} is ready!')
    check_inactive.start()

@tasks.loop(minutes=1)
async def check_inactive():
    """Check for inactive voice clients and disconnect"""
    for guild_id, player in list(music_players.items()):
        vc = player.voice_client
        if vc and vc.is_connected():
            idle_for = datetime.now() - player.last_activity
            # Disconnect if idle for > 5 minutes and not playing/paused
            if idle_for > timedelta(minutes=5) and not (vc.is_playing() or vc.is_paused()):
                await vc.disconnect()
                music_players.pop(guild_id, None)

@bot.command()
async def play(ctx, *, query):
    """Play a song from YouTube, Spotify, or search query"""
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("You need to be in a voice channel!")
        return

    player = get_player(ctx.guild.id)

    # Connect once and keep the voice client
    if not player.voice_client or not player.voice_client.is_connected():
        player.voice_client = await ctx.author.voice.channel.connect()

    await ctx.send(f"🔍 Searching for: **{query}**")

    # Spotify handling: resolve to YouTube by title
    if 'spotify.com' in query:
        try:
            spotify_info = extract_spotify_info(query)
            if not spotify_info:
                await ctx.send("❌ Could not parse Spotify link.")
                return

            if spotify_info['type'] == 'track':
                info = await search_youtube_first(spotify_info['name'])
                if info:
                    source = await get_audio_source(info.get('webpage_url') or spotify_info['name'])
                    if source:
                        player.queue.append(source)
                        await ctx.send(f"✅ Added to queue: **{source['title']}**")
                    else:
                        await ctx.send("❌ Could not get a playable stream for that track.")
                else:
                    await ctx.send("❌ Could not find that track on YouTube.")
            else:
                # Playlist or album
                names = spotify_info.get('tracks', [])
                await ctx.send(f"📝 Adding {len(names)} tracks from **{spotify_info['name']}**...")
                added = 0
                for track_name in names:
                    info = await search_youtube_first(track_name)
                    if info:
                        source = await get_audio_source(info.get('webpage_url') or track_name)
                        if source:
                            player.queue.append(source)
                            added += 1
                await ctx.send(f"✅ Added {added} tracks to queue!")
        except Exception as e:
            await ctx.send(f"❌ Error processing Spotify link: {str(e)}")
            return

    # YouTube playlist URL
    elif is_url(query) and 'list=' in query:
        try:
            with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
                playlist_info = ydl.extract_info(query, download=False)
            entries = playlist_info.get('entries', []) if playlist_info else []
            await ctx.send(f"📝 Adding {len(entries)} videos from playlist...")
            added = 0
            for entry in entries:
                if not entry:
                    continue
                # Use webpage_url (canonical) to re-extract a playable stream
                page = entry.get('webpage_url') or entry.get('url')
                source = await get_audio_source(page)
                if source:
                    player.queue.append(source)
                    added += 1
            await ctx.send(f"✅ Added {added} videos to queue!")
        except Exception as e:
            await ctx.send(f"❌ Error processing playlist: {str(e)}")
            return

    # Direct YouTube URL or plain search query
    else:
        source = await get_audio_source(query)
        if source:
            player.queue.append(source)
            await ctx.send(f"✅ Added to queue: **{source['title']}**")
        else:
            await ctx.send("❌ Could not find a playable stream for that query.")
            return

    player.last_activity = datetime.now()

    # Start playback if idle
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
        player.current = None
        music_players.pop(ctx.guild.id, None)
        await ctx.send("👋 Left the voice channel")
    else:
        await ctx.send("I'm not in a voice channel!")

@bot.command(name='commands', aliases=['help', 'h'])
async def commands_list(ctx):
    embed = discord.Embed(title="🎵 Music Bot Commands", color=discord.Color.green())
    embed.add_field(name="!play <song/url>", value="Play a song from YouTube, Spotify, or search", inline=False)
    embed.add_field(name="!pause", value="Pause the current song", inline=False)
    embed.add_field(name="!resume", value="Resume the paused song", inline=False)
    embed.add_field(name="!skip", value="Skip the current song", inline=False)
    embed.add_field(name="!stop", value="Stop and clear queue", inline=False)
    embed.add_field(name="!loop", value="Toggle loop mode", inline=False)
    embed.add_field(name="!queue", value="Show current queue", inline=False)
    embed.add_field(name="!leave", value="Leave voice channel", inline=False)
    await ctx.send(embed=embed)

# Web server for Render
async def handle_health(request):
    return web.Response(text="Bot is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_health)
    app.router.add_get('/health', handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv('PORT', 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"Web server started on port {port}")

async def main():
    async with bot:
        await start_web_server()
        token = os.getenv('DISCORD_TOKEN')
        if not token:
            raise RuntimeError("DISCORD_TOKEN is not set")
        await bot.start(token)

if __name__ == "__main__":
    asyncio.run(main())
