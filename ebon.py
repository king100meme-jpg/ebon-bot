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

# روم الصوت الذي سيدخله البوت تلقائياً 24/7
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
# تشغيل البوت ودخول الروم الصوتي تلقائياً
# ==========================================

@bot.event
async def on_ready():
    print(f"تم تشغيل البوت بنجاح باسم: {bot.user}")
    await bot.change_presence(
        activity=discord.Game(
            name="!help | خدمة السيرفر"
        )
    )
    
    # الانتظار لمدة 3 ثوانٍ لضمان تحميل ديسكورد للرومات بالكامل
    await asyncio.sleep(3)
    
    try:
        channel = bot.get_channel(VOICE_CHANNEL_ID)
        if channel and isinstance(channel, discord.VoiceChannel):
            if not discord.utils.get(bot.voice_clients, guild=channel.guild):
                await channel.connect()
                print(f"✅ دخل البوت الروم الصوتي بنجاح: {channel.name}")
            else:
                print("⚠️ البوت متصل مسبقاً بالروم الصوتي.")
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
# البرودكاست
# ==========================================

@bot.command(
    name="bc",
    aliases=["broadcast"]
)
@commands.has_permissions(administrator=True)
async def broadcast(ctx, *, msg: str = None):
    if not msg:
        await ctx.reply(
            "❌ يرجى كتابة الرسالة المراد إرسالها!"
        )
        return

    status_msg = await ctx.send(
        "⏳ جاري إرسال البرودكاست..."
    )

    success_count = 0
    fail_count = 0

    embed = discord.Embed(
        title=f"📢 رسالة إدارية من {ctx.guild.name}",
        description=msg,
        color=discord.Color.blue()
    )
    embed.set_footer(
        text=f"أُرسلت بواسطة {ctx.author}"
    )

    for member in ctx.guild.members:
        if member.bot:
            continue
        try:
            await member.send(embed=embed)
            success_count += 1
        except (discord.Forbidden, discord.HTTPException):
            fail_count += 1

    await status_msg.edit(
        content=(
            "✅ اكتمل البرودكاست:\n"
            f"**تم الإرسال لـ:** {success_count} عضو\n"
            f"**فشل الإرسال لـ:** {fail_count} عضو"
        )
    )

@broadcast.error
async def broadcast_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.reply(
            "❌ لا تمتلك صلاحية استخدام هذا الأمر!"
        )

# ==========================================
# الألعاب
# ==========================================

@bot.command(name="rps")
async def rps(ctx, choice: str = None):
    choices = ["حجر", "ورقة", "مقص"]
    if choice not in choices:
        await ctx.reply(
            "❌ اختر: !rps حجر أو !rps ورقة أو !rps مقص"
        )
        return

    bot_choice = random.choice(choices)
    if choice == bot_choice:
        await ctx.reply(
            f"تعادل! 🤝 اخترت **{choice}** وأنا اخترت **{bot_choice}**"
        )
        return

    win_rules = {
        "حجر": "مقص",
        "ورقة": "حجر",
        "مقص": "ورقة"
    }

    if win_rules[choice] == bot_choice:
        await ctx.reply(
            f"مبروك فزت! 🎉 اخترت **{choice}** وأنا اخترت **{bot_choice}**"
        )
    else:
        await ctx.reply(
            f"خسرت! اخترت **{choice}** وأنا اخترت **{bot_choice}**"
        )

@bot.command(name="guess")
async def guess(ctx, number: int = None):
    secret_num = random.randint(1, 10)
    if number is None or number < 1 or number > 10:
        await ctx.reply(
            "❌ خمن رقماً بين 1 و 10! مثال: !guess 5"
        )
        return

    if number == secret_num:
        await ctx.reply(
            f"🎯 إجابة صحيحة! الرقم هو **{secret_num}**"
        )
    else:
        await ctx.reply(
            f"❌ إجابة خاطئة! الرقم الصحيح كان **{secret_num}**"
        )
