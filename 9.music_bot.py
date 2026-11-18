import asyncio
import os
import discord
from discord.ext import commands
import yt_dlp

# ==========================================
# إعدادات بوت الأغاني
# ==========================================

COMMAND_PREFIX = "!"

# 🟢 حط هنا "أيدي الروم الصوتي" اللي نسخته (انسخ الرقم وحطه بين العلامتين)
VOICE_CHANNEL_ID = 1559179176785874954

# Discord Intents (بوت الأغاني يحتاج صلاحيات الاتصال والصوت)
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(
    command_prefix=COMMAND_PREFIX,
    intents=intents
)

# ==========================================
# حدث التشغيل والبقاء في الروم التلقائي
# ==========================================

@bot.event
async def on_ready():
    print(f"تم تشغيل بوت الأغاني بنجاح باسم: {bot.user}")
    await bot.change_presence(
        activity=discord.Game(
            name="!ش [اسم الأغنية]"
        )
    )
    
    # انتظار قصير لضمان استقرار الاتصال
    await asyncio.sleep(5)
    
    try:
        channel = bot.get_channel(VOICE_CHANNEL_ID)
        if channel and isinstance(channel, discord.VoiceChannel):
            # التأكد من قطع أي اتصال سابق لمنع الأخطاء
            for vc in bot.voice_clients:
                if vc.guild == channel.guild:
                    await vc.disconnect()
            
            # الدخول للروم والثبات فيه
            await channel.connect()
            print(f"✅ دخل البوت الروم الصوتي بنجاح وثبت فيه: {channel.name}")
        else:
            print(f"❌ لم يتم العثور على الروم الصوتي بالرقم: {VOICE_CHANNEL_ID}")
    except Exception as e:
        print(f"❌ خطأ أثناء محاولة دخول الروم الصوتي: {e}")

# ==========================================
# نظام الأغاني والتحكم الصوتي
# ==========================================

FFMPEG_OPTIONS = {
    "before_options": (
        "-reconnect 1 -reconnect_streamed 1 "
        "-reconnect_delay_max 5"
    ),
    "options": "-vn"
}

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)

@bot.command(name="دخول", aliases=["join"])
async def join_vc(ctx):
    if not ctx.author.voice:
        await ctx.reply("❌ يجب أن تكون متواجدًا في روم صوتي لكي أتمكن من الدخول معك!")
        return
    channel = ctx.author.voice.channel
    try:
        if ctx.voice_client is not None:
            await ctx.voice_client.move_to(channel)
        else:
            await channel.connect()
        await ctx.reply(f"✅ تم الدخول بنجاح إلى الروم: **{channel.name}**")
    except Exception as e:
        await ctx.reply(f"❌ حدث خطأ أثناء محاولة دخول الروم: {e}")

@bot.command(name="ش")
async def play(ctx, *, url_or_search: str = None):
    if ctx.author.voice:
        channel = ctx.author.voice.channel
        try:
            if ctx.voice_client is None:
                await channel.connect()
            elif ctx.voice_client.channel != channel:
                await ctx.voice_client.move_to(channel)
        except Exception:
            pass

    if not url_or_search:
        await ctx.reply("❌ اكتب اسم الأغنية أو الرابط! (مثال: !ش [اسم الأغنية أو الرابط])")
        return

    try:
        async with ctx.typing():
            loop = asyncio.get_running_loop()
            if url_or_search.startswith("http://") or url_or_search.startswith("https://"):
                search_query = url_or_search
            else:
                search_query = f"ytsearch1:{url_or_search}"

            data = await loop.run_in_executor(
                None,
                lambda: ytdl.extract_info(search_query, download=False)
            )

        if "entries" in data:
            if not data["entries"]:
                await ctx.reply("❌ ما حصلت الأغنية أو الرابط غير صالح.")
                return
            song = data["entries"][0]
        else:
            song = data

        song_url = song["url"]
        title = song.get("title", "مقطع صوتي")
        voice_client = ctx.voice_client

        if voice_client is None:
            await ctx.reply("❌ تعذر اتصال البوت بالروم الصوتي.")
            return

        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()

        source = discord.FFmpegPCMAudio(song_url, **FFMPEG_OPTIONS)
        voice_client.play(source)

        await ctx.reply(f"🎵 جاري تشغيل: **{title}**")
    except Exception as error:
        print(f"Music error: {error}")
        await ctx.reply("❌ صار خطأ أثناء تشغيل الأغنية. تأكد من صحة الرابط أو تثبيت FFmpeg.")

@bot.command(name="وقف")
async def stop_playing(ctx):
    if ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
        ctx.voice_client.stop()
        await ctx.reply("⏹️ تم إيقاف الأغنية.")
    else:
        await ctx.reply("❌ لا توجد أغنية تعمل حالياً!")

@bot.command(name="طلوع")
async def leave_vc(ctx):
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.reply("👋 تم إيقاف التشغيل ومغادرة الروم الصوتي.")
    else:
        await ctx.reply("❌ البوت ليس متصلاً بأي روم صوتي!")

# ==========================================
# تشغيل البوت
# ==========================================
TOKEN = os.getenv("TOKEN_BOT_9")

if TOKEN is None:
    print("❌ خطأ: لم يتم العثور على التوكن!")
else:
    bot.run(TOKEN)