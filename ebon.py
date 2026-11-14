import asyncio
import os
import random
import time

import discord
from discord.ext import commands
import yt_dlp

# ==========================================
# إعدادات البوت
# ==========================================

COMMAND_PREFIX = "!"
LOG_CHANNEL_NAME = "logs"

# روم الرياكشن التلقائي
REACTION_CHANNEL_ID = 1559179188987109527

# رقم الإيموجي المخصص
REACTION_EMOJI_ID = 1564627151246856362

# روم الصوت الذي سيدخله البوت تلقائياً ويبقى فيه 24 ساعة
VOICE_CHANNEL_ID = 1559179127389691945

# منع السبام
message_log = {}

# Discord Intents
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(
    command_prefix=COMMAND_PREFIX,
    intents=intents
)

# ==========================================
# أدوات مساعدة
# ==========================================

def get_reaction_emoji(guild):
    emoji = guild.get_emoji(REACTION_EMOJI_ID)
    if emoji is not None:
        return emoji
    return discord.PartialEmoji(
        name="emoji",
        id=REACTION_EMOJI_ID
    )

def get_log_channel(guild):
    return discord.utils.get(
        guild.text_channels,
        name=LOG_CHANNEL_NAME
    )

# ==========================================
# تشغيل البوت والبقاء في الروم الصوتي 24 ساعة
# ==========================================

@bot.event
async def on_ready():
    print(f"تم تشغيل البوت بنجاح باسم: {bot.user}")
    await bot.change_presence(
        activity=discord.Game(
            name="انا من صنع العم جنتل"
        )
    )
    
    # انتظار قصير لضمان استقرار الاتصال بالديسكورد
    await asyncio.sleep(5)
    
    try:
        channel = bot.get_channel(VOICE_CHANNEL_ID)
        if channel and isinstance(channel, discord.VoiceChannel):
            # التأكد من قطع أي اتصال سابق عالق لمنع خطأ 4006
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
# الحماية + الرياكشن التلقائي
# ==========================================

@bot.event
async def on_message(message):
    if message.author.bot or message.guild is None:
        await bot.process_commands(message)
        return

    # الرياكشن التلقائي
    if message.channel.id == REACTION_CHANNEL_ID:
        try:
            emoji = get_reaction_emoji(message.guild)
            await message.add_reaction(emoji)
        except discord.HTTPException as error:
            print(f"خطأ الرياكشن: {error}")

    # منع الروابط باستثناء الإداريين
    content_lower = message.content.lower()
    if (
        "http://" in content_lower
        or "https://" in content_lower
        or "www." in content_lower
    ):
        if not message.author.guild_permissions.administrator:
            try:
                await message.delete()
            except discord.HTTPException:
                pass

            try:
                await message.channel.send(
                    f"⚠️ {message.author.mention}، يمنع إرسال الروابط في هذا السيرفر!"
                )
            except discord.HTTPException:
                pass
            return

    # منع السبام
    user_id = (
        message.guild.id,
        message.author.id
    )
    current_time = time.time()
    last_time, count = message_log.get(
        user_id,
        (0, 0)
    )

    if current_time - last_time < 2:
        count += 1
    else:
        count = 1

    message_log[user_id] = (
        current_time,
        count
    )

    if count > 4:
        try:
            await message.delete()
        except discord.HTTPException:
            pass

        try:
            await message.channel.send(
                f"⚠️ {message.author.mention}، يرجى التوقف عن التكرار والسبام!"
            )
        except discord.HTTPException:
            pass
        return

    await bot.process_commands(message)

# ==========================================
# أوامر البرودكاست (نص عادي مع منشن المرسل)
# ==========================================

# 1. برودكاست للأعضاء (!bc)
@bot.command(
    name="bc",
    aliases=["broadcast"]
)
@commands.has_permissions(administrator=True)
async def broadcast(ctx, *, msg: str = None):
    if not msg:
        await ctx.reply("❌ يرجى كتابة الرسالة المراد إرسالها للأعضاء!")
        return

    status_msg = await ctx.send("⏳ جاري إرسال البرودكاست للأعضاء...")

    success_count = 0
    fail_count = 0

    formatted_msg = f"{msg}\n\n— المرسل: {ctx.author.mention}"

    for member in ctx.guild.members:
        if member.bot:
            continue
        try:
            await member.send(formatted_msg)
            success_count += 1
        except (discord.Forbidden, discord.HTTPException):
            fail_count += 1

    await status_msg.edit(
        content=(
            "✅ اكتمل برودكاست الأعضاء:\n"
            f"**تم الإرسال لـ:** {success_count} عضو\n"
            f"**فشل الإرسال لـ:** {fail_count} عضو"
        )
    )

# 2. برودكاست للإدارة فقط (!bca)
@bot.command(
    name="bca",
    aliases=["adminbc"]
)
@commands.has_permissions(administrator=True)
async def admin_broadcast(ctx, *, msg: str = None):
    if not msg:
        await ctx.reply("❌ يرجى كتابة الرسالة المراد إرسالها للإدارة!")
        return

    status_msg = await ctx.send("⏳ جاري إرسال البرودكاست للإداريين فقط...")

    success_count = 0
    fail_count = 0

    formatted_msg = f"🔒 **[رسالة إدارية]**\n{msg}\n\n— المرسل: {ctx.author.mention}"

    for member in ctx.guild.members:
        if member.bot or not member.guild_permissions.administrator:
            continue
        try:
            await member.send(formatted_msg)
            success_count += 1
        except (discord.Forbidden, discord.HTTPException):
            fail_count += 1

    await status_msg.edit(
        content=(
            "✅ اكتمل برودكاست الإدارة:\n"
            f"**تم الإرسال لـ:** {success_count} إداري\n"
            f"**فشل الإرسال لـ:** {fail_count} إداري"
        )
    )

@broadcast.error
@admin_broadcast.error
async def broadcast_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.reply("❌ لا تمتلك صلاحية استخدام هذا الأمر!")

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
        await ctx.reply(
            "❌ اكتب اسم الأغنية أو الرابط (يوتيوب / ساوند كلاود)! (مثال: !ش [الرابط] أو اسم الأغنية)"
        )
        return

    try:
        async with ctx.typing():
            loop = asyncio.get_running_loop()
            
            # التحقق إذا كان المدخل رابط مباشر أو نص بحث
            if url_or_search.startswith("http://") or url_or_search.startswith("https://"):
                search_query = url_or_search
            else:
                search_query = f"ytsearch1:{url_or_search}"

            data = await loop.run_in_executor(
                None,
                lambda: ytdl.extract_info(
                    search_query,
                    download=False
                )
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
            await ctx.reply(
                "❌ تعذر اتصال البوت بالروم الصوتي."
            )
            return

        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()

        source = discord.FFmpegPCMAudio(
            song_url,
            **FFMPEG_OPTIONS
        )
        voice_client.play(source)

        await ctx.reply(
            f"🎵 جاري تشغيل: **{title}**"
        )
    except Exception as error:
        print(f"Music error: {error}")
        await ctx.reply(
            "❌ صار خطأ أثناء تشغيل الأغنية. تأكد من صحة الرابط أو تثبيت FFmpeg."
        )

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
# نظام اللوقات (حذف رسائل فقط)
# ==========================================

@bot.event
async def on_message_delete(message):
    if message.guild is None:
        return
    if message.author and message.author.bot:
        return

    log_channel = get_log_channel(message.guild)
    if log_channel is None:
        return

    embed = discord.Embed(
        title="🗑️ تم حذف رسالة",
        color=discord.Color.red()
    )
    if message.author:
        embed.add_field(
            name="المرسل:",
            value=str(message.author),
            inline=True
        )

    embed.add_field(
        name="الروم:",
        value=getattr(message.channel, "mention", "غير معروف"),
        inline=True
    )
    embed.add_field(
        name="المحتوى:",
        value=(
            message.content[:1000]
            if message.content
            else "محتوى غير نصي (صورة/ملف)"
        ),
        inline=False
    )
    embed.timestamp = discord.utils.utcnow()

    try:
        await log_channel.send(embed=embed)
    except discord.HTTPException:
        pass

# ==========================================
# تشغيل البوت
# ==========================================

TOKEN = os.getenv("DISCORD_TOKEN")

if TOKEN is None:
    print("❌ خطأ: لم يتم العثور على التوكن في متغيرات البيئة (Railway Variables)!")
else:
    bot.run(TOKEN)
