import discord
from discord.ext import commands, tasks
import asyncio
import os
import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from collections import deque
import re
from datetime import datetime, timedelta

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

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

YTDL_OPTIONS = {
    'format': 'bestaudio/best',
    'extractaudio': True,
    'audioformat': 'mp3',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': False,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'extract_flat': 'in_playlist'
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

def get_player(guild_id):
    if guild_id not in music_players:
        music_players[guild_id] = MusicPlayer()
    return music_players[guild_id]

def extract_spotify_info(url):
    """Extract track info from Spotify URL"""
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
            track = item['track']
            tracks.append(f"{track['artists'][0]['name']} - {track['name']}")
        return {
            'type': 'playlist',
            'tracks': tracks,
            'name': playlist['name']
        }
    elif 'album' in url:
        album_id = url.split('album/')[1].split('?')[0]
        album = sp.album(album_id)
        tracks = []
        for track in album['tracks']['items']:
            tracks.append(f"{track['artists'][0]['name']} - {track['name']}")
        return {
            'type': 'album',
            'tracks': tracks,
            'name': album['name']
        }
    return None

async def search_youtube(query):
    """Search YouTube for a song"""
    with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
        try:
            info = ydl.extract_info(f"ytsearch:{query}", download=False)
            if 'entries' in info:
                return info['entries'][0]
            return info
        except Exception as e:
            print(f"Error searching: {e}")
            return None

async def get_audio_source(url_or_query):
    """Get audio source from URL or search query"""
    with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
        try:
            info = ydl.extract_info(url_or_query, download=False)
            if 'entries' in info:
                # Playlist
                return info['entries']
            url = info['url']
            title = info.get('title', 'Unknown')
            return {'url': url, 'title': title, 'webpage_url': info.get('webpage_url', url_or_query)}
        except Exception as e:
            print(f"Error getting audio: {e}")
            return None

def play_next(guild_id):
    """Play next song in queue"""
    player = get_player(guild_id)
    
    if player.loop and player.current:
        player.queue.appendleft(player.current)
    
    if not player.queue:
        player.current = None
        return
    
    player.current = player.queue.popleft()
    player.last_activity = datetime.now()
    
    try:
        source = discord.FFmpegPCMAudio(player.current['url'], **FFMPEG_OPTIONS)
        player.voice_client.play(source, after=lambda e: after_song(guild_id, e))
    except Exception as e:
        print(f"Error playing: {e}")
        play_next(guild_id)

def after_song(guild_id, error):
    """Called after a song finishes"""
    if error:
        print(f"Player error: {error}")
    
    coro = continue_playback(guild_id)
    asyncio.run_coroutine_threadsafe(coro, bot.loop)

async def continue_playback(guild_id):
    """Continue to next song"""
    await asyncio.sleep(1)
    play_next(guild_id)

@bot.event
async def on_ready():
    print(f'{bot.user} is ready!')
    check_inactive.start()

@tasks.loop(minutes=1)
async def check_inactive():
    """Check for inactive voice clients and disconnect"""
    for guild_id, player in list(music_players.items()):
        if player.voice_client and player.voice_client.is_connected():
            if datetime.now() - player.last_activity > timedelta(minutes=5):
                await player.voice_client.disconnect()
                music_players.pop(guild_id, None)

@bot.command()
async def play(ctx, *, query):
    """Play a song from YouTube, Spotify, or search query"""
    if not ctx.author.voice:
        await ctx.send("You need to be in a voice channel!")
        return
    
    player = get_player(ctx.guild.id)
    
    if not player.voice_client:
        player.voice_client = await ctx.author.voice.channel.connect()
    
    await ctx.send(f"🔍 Searching for: **{query}**")
    
    # Check if Spotify URL
    if 'spotify.com' in query:
        try:
            spotify_info = extract_spotify_info(query)
            if spotify_info['type'] == 'track':
                info = await search_youtube(spotify_info['name'])
                if info:
                    source = await get_audio_source(info['webpage_url'])
                    player.queue.append(source)
                    await ctx.send(f"✅ Added to queue: **{source['title']}**")
            else:
                # Playlist or album
                await ctx.send(f"📝 Adding {len(spotify_info['tracks'])} tracks from **{spotify_info['name']}**...")
                for track_name in spotify_info['tracks']:
                    info = await search_youtube(track_name)
                    if info:
                        source = await get_audio_source(info['webpage_url'])
                        player.queue.append(source)
                await ctx.send(f"✅ Added {len(spotify_info['tracks'])} tracks to queue!")
        except Exception as e:
            await ctx.send(f"❌ Error processing Spotify link: {str(e)}")
            return
    
    # Check if YouTube playlist
    elif 'list=' in query:
        try:
            with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
                playlist_info = ydl.extract_info(query, download=False)
                if 'entries' in playlist_info:
                    await ctx.send(f"📝 Adding {len(playlist_info['entries'])} videos from playlist...")
                    for entry in playlist_info['entries']:
                        if entry:
                            source = await get_audio_source(entry['webpage_url'])
                            if source:
                                player.queue.append(source)
                    await ctx.send(f"✅ Added {len(playlist_info['entries'])} videos to queue!")
        except Exception as e:
            await ctx.send(f"❌ Error processing playlist: {str(e)}")
            return
    
    # YouTube URL or search query
    else:
        source = await get_audio_source(query)
        if source:
            player.queue.append(source)
            await ctx.send(f"✅ Added to queue: **{source['title']}**")
        else:
            await ctx.send("❌ Could not find the song!")
            return
    
    player.last_activity = datetime.now()
    
    if not player.voice_client.is_playing():
        play_next(ctx.guild.id)

@bot.command()
async def pause(ctx):
    """Pause the current song"""
    player = get_player(ctx.guild.id)
    if player.voice_client and player.voice_client.is_playing():
        player.voice_client.pause()
        await ctx.send("⏸️ Paused")
    else:
        await ctx.send("Nothing is playing!")

@bot.command()
async def resume(ctx):
    """Resume the paused song"""
    player = get_player(ctx.guild.id)
    if player.voice_client and player.voice_client.is_paused():
        player.voice_client.resume()
        await ctx.send("▶️ Resumed")
    else:
        await ctx.send("Nothing is paused!")

@bot.command()
async def skip(ctx):
    """Skip the current song"""
    player = get_player(ctx.guild.id)
    if player.voice_client and player.voice_client.is_playing():
        player.voice_client.stop()
        await ctx.send("⏭️ Skipped")
    else:
        await ctx.send("Nothing is playing!")

@bot.command()
async def stop(ctx):
    """Stop playing and clear the queue"""
    player = get_player(ctx.guild.id)
    player.queue.clear()
    player.current = None
    if player.voice_client:
        player.voice_client.stop()
    await ctx.send("⏹️ Stopped and cleared queue")

@bot.command()
async def loop(ctx):
    """Toggle loop mode"""
    player = get_player(ctx.guild.id)
    player.loop = not player.loop
    status = "enabled" if player.loop else "disabled"
    await ctx.send(f"🔁 Loop {status}")

@bot.command()
async def queue(ctx):
    """Show the current queue"""
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
    """Leave the voice channel"""
    player = get_player(ctx.guild.id)
    if player.voice_client:
        await player.voice_client.disconnect()
        player.queue.clear()
        player.current = None
        music_players.pop(ctx.guild.id, None)
        await ctx.send("👋 Left the voice channel")
    else:
        await ctx.send("I'm not in a voice channel!")

@bot.command()
async def help(ctx):
    """Show help message"""
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

bot.run(os.getenv('DISCORD_TOKEN'))
