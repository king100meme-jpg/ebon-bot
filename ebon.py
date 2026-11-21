import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import os
import re
import time

import aiosqlite
import discord
from discord.ext import commands, tasks
from google import genai
from google.genai import types

# ==========================================
# 1. الإعدادات الأساسية
# ==========================================

COMMAND_PREFIX = "!"
DB_PATH = os.getenv("GUARD_DB_PATH", "fox_guard.db")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# الرياكشن التلقائي
REACTION_CHANNEL_ID = 1559179188987109527
REACTION_EMOJI_IDS = [
    1564627151246856362,
    1567208015491244233,
    1564620348295614495,
]

# الروم الصوتي للثبات 24 ساعة
VOICE_CHANNEL_ID = 1559179127389691945
VOICE_CHECK_INTERVAL = 60

# روم لوق محاولات التخريب (لوق-تخريب)
NUKE_LOG_CHANNEL_ID = 1573623918839992370

# آيدي الأب اللي يتكلم معاه البوت ويناديه بابا
OWNER_FATHER_ID = 292784680330330112

ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

AI_SYSTEM_PROMPT = (
    "أنت 'فوكس'، حارس السيرفر ومساعده الذكي. "
    "أسلوبك خشن وله هيبة وثقة عالية، بلهجة خليجية شبابية حماسية، لكنك خفيف، ذكي، سريع بديهة، وإيجابي ومفعم بالطاقة—أبداً مو سلبي ولا ثقيل دم ولا كئيب. "
    "إذا كان المتحدث هو أبوك، تناديه دايم 'بابا' وتحترمه وتنفّذ أوامره وتجاوبه بنفس الهيبة والخشونة المحببة مع روح حماسية وخفة ظل بدون مياعة. "
    "إذا كان المتحدث شخص ثاني غير أبوك، ترد عليه بجلافة وبرود خفيف ومختصر وتوضح له إنك ما تسولف إلا مع أبوك، بدون سب أو قذف أو مخالفات لقوانين دسكورد."
)

intents = discord.Intents.all()

DEFAULT_CFG = {
    "lang": "ar",
    "log_channel_id": 0,
    "panic": False,
    "panic_prev_perms": None,
    "nuke_limit": 1,
    "raid_joins": 7,
    "raid_window": 10,
    "spam_msgs": 5,
    "spam_window": 4,
    "timeout_minutes": 30,
    "modules": {
        "anti_nuke": True,
        "anti_raid": True,
        "anti_admin": True,
        "anti_role": True,
        "anti_channel": True,
        "anti_bot": True,
        "anti_spam": True,
        "anti_links": True,
    },
    "trusted_users": [],
    "trusted_bots": [],
    "protected_roles": [],
    "ignored_channels": [],
}

# ==========================================
# 2. قاعدة البيانات (خفيفة وسريعة)
# ==========================================

class GuardDB:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def init(self):
        async with self._lock:
            self.conn = await aiosqlite.connect(self.path)
            await self.conn.execute("PRAGMA journal_mode=WAL;")
            await self.conn.execute("PRAGMA synchronous=NORMAL;")
            await self.conn.execute("""
                CREATE TABLE IF NOT EXISTS configs (
                    guild_id INTEGER PRIMARY KEY,
                    data TEXT NOT NULL
                )
            """)
            await self.conn.execute("""
                CREATE TABLE IF NOT EXISTS backups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    snapshot TEXT NOT NULL
                )
            """)
            await self.conn.commit()

    async def close(self):
        async with self._lock:
            if self.conn:
                await self.conn.close()
                self.conn = None

    async def get_cfg(self, guild_id: int) -> dict:
        async with self._lock:
            async with self.conn.execute("SELECT data FROM configs WHERE guild_id = ?", (guild_id,)) as cur:
                row = await cur.fetchone()
        cfg = json.loads(json.dumps(DEFAULT_CFG))
        if row:
            try:
                loaded = json.loads(row[0])
                cfg.update({k: v for k, v in loaded.items() if k != "modules"})
                cfg["modules"].update(loaded.get("modules", {}))
            except json.JSONDecodeError:
                await self.save_cfg(guild_id, cfg)
        else:
            await self.save_cfg(guild_id, cfg)
        return cfg

    async def save_cfg(self, guild_id: int, cfg: dict):
        payload = json.dumps(cfg)
        async with self._lock:
            await self.conn.execute(
                "INSERT OR REPLACE INTO configs (guild_id, data) VALUES (?, ?)",
                (guild_id, payload),
            )
            await self.conn.commit()

    async def save_backup(self, guild_id: int, snap: dict) -> int:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        payload = json.dumps(snap)
        async with self._lock:
            cur = await self.conn.execute(
                "INSERT INTO backups (guild_id, created_at, snapshot) VALUES (?, ?, ?)",
                (guild_id, stamp, payload),
            )
            await self.conn.execute(
                "DELETE FROM backups WHERE guild_id = ? AND id NOT IN (SELECT id FROM backups WHERE guild_id = ? ORDER BY id DESC LIMIT 10)",
                (guild_id, guild_id),
            )
            await self.conn.commit()
            return cur.lastrowid

    async def get_backup(self, guild_id: int, backup_id: int | None = None):
        if backup_id is not None:
            q, p = "SELECT snapshot FROM backups WHERE guild_id = ? AND id = ?", (guild_id, backup_id)
        else:
            q, p = "SELECT snapshot FROM backups WHERE guild_id = ? ORDER BY id DESC LIMIT 1", (guild_id,)
        async with self._lock:
            async with self.conn.execute(q, p) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return None

# ==========================================
# 3. محرك الحماية والذاكرة المؤقتة
# ==========================================

class FoxGuardBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=COMMAND_PREFIX, intents=intents)
        self.db = GuardDB(DB_PATH)
        self.cfg_cache: dict[int, tuple[float, dict]] = {}
        self.nuke_hits: dict[tuple, list[float]] = defaultdict(list)
        self.joins: dict[int, list[float]] = defaultdict(list)
        self.spam: dict[tuple, list[float]] = defaultdict(list)
        self.recent_punished: set[tuple] = set()
        self.actor_locks: dict[tuple, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.voice_lock = asyncio.Lock()

    async def setup_hook(self):
        await self.db.init()
        self.cleanup_task.start()
        self.keep_voice_alive_task.start()

    async def close(self):
        self.cleanup_task.cancel()
        self.keep_voice_alive_task.cancel()
        await self.db.close()
        await super().close()

    async def cfg(self, guild_id: int, force_reload: bool = False) -> dict:
        now = time.time()
        cached = self.cfg_cache.get(guild_id)
        if not force_reload and cached and (now - cached[0]) < 3.0:
            return cached[1]
        data = await self.db.get_cfg(guild_id)
        self.cfg_cache[guild_id] = (now, data)
        return data

    async def set_cfg(self, guild_id: int, data: dict):
        await self.db.save_cfg(guild_id, data)
        self.cfg_cache[guild_id] = (time.time(), data)

    @tasks.loop(minutes=5)
    async def cleanup_task(self):
        now = time.time()
        for k in list(self.nuke_hits.keys()):
            self.nuke_hits[k] = [t for t in self.nuke_hits[k] if now - t < 30]
            if not self.nuke_hits[k]:
                del self.nuke_hits[k]
        for gid in list(self.joins.keys()):
            self.joins[gid] = [t for t in self.joins[gid] if now - t < 30]
            if not self.joins[gid]:
                del self.joins[gid]
        for k in list(self.spam.keys()):
            self.spam[k] = [t for t in self.spam[k] if now - t < 15]
            if not self.spam[k]:
                del self.spam[k]

    @tasks.loop(seconds=VOICE_CHECK_INTERVAL)
    async def keep_voice_alive_task(self):
        await self.wait_until_ready()
        await ensure_voice_channel()

bot = FoxGuardBot()

# ==========================================
# 4. أدوات التنفيذ السريع والذكاء الاصطناعي واللوق
# ==========================================

async def generate_rough_reply(user_name: str, prompt_text: str, is_father: bool) -> str:
    if not ai_client:
        return "بابا، مفتاح الذكاء الاصطناعي مو مضاف في الخادم الحين، نركّبه ونولّعها." if is_father else "ما أتكلم إلا مع أبوي."

    role_context = (
        f"المتحدث هو أبوك ({user_name})، نادِه 'بابا' وجاوبه بحماس وهيبة وخفة بدون سلبية أو ثقل: {prompt_text}"
        if is_father
        else f"المتحدث شخص عادي ({user_name}) وليس أبوك، رد عليه ببرود وجلافة مختصرة إنك ما تتكلم إلا مع أبوك: {prompt_text}"
    )

    max_retries = 5
    for attempt in range(max_retries):
        try:
            response = await asyncio.wait_for(
                ai_client.aio.models.generate_content(
                    model="gemini-3.8-flash",
                    contents=role_context,
                    config=types.GenerateContentConfig(
                        system_instruction=AI_SYSTEM_PROMPT,
                        temperature=0.85,
                        max_output_tokens=250,
                    ),
                ),
                timeout=15.0,
            )
            text = (response.text or "").strip()
            if text:
                return text[:1900]
        except asyncio.TimeoutError:
            print(f"AI Error: Timeout on gemini-3.8-flash (attempt {attempt + 1}/{max_retries})")
            if attempt < max_retries - 1:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
        except Exception as e:
            err_str = str(e)
            print(f"AI Error: gemini-3.8-flash (attempt {attempt + 1}/{max_retries}) -> {err_str}")
            if ("503" in err_str or "UNAVAILABLE" in err_str or "429" in err_str) and attempt < max_retries - 1:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            break

    return "بابا، سيرفرات قوقل مضغوطة حالياً، عطني شوي وجرّب مرة ثانية." if is_father else "ما عندي كلام لك."

def get_reaction_emojis(guild: discord.Guild):
    emojis = []
    for eid in REACTION_EMOJI_IDS:
        em = guild.get_emoji(eid)
        emojis.append(em if em is not None else discord.PartialEmoji(name="emoji", id=eid))
    return emojis

async def is_trusted(guild: discord.Guild, user: discord.abc.User | None) -> bool:
    if not user:
        return False
    if user.id == guild.owner_id or (bot.user and user.id == bot.user.id) or (OWNER_FATHER_ID and user.id == OWNER_FATHER_ID):
        return True
    data = await bot.cfg(guild.id)
    return user.id in data["trusted_users"] or user.id in data["trusted_bots"]

async def diag_log(guild: discord.Guild, text: str):
    """لوق محاولات التخريب والأحداث الأمنية"""
    ch = None
    if NUKE_LOG_CHANNEL_ID:
        ch = bot.get_channel(NUKE_LOG_CHANNEL_ID) or guild.get_channel(NUKE_LOG_CHANNEL_ID)
    if not isinstance(ch, discord.TextChannel):
        data = await bot.cfg(guild.id)
        ch = guild.get_channel(data["log_channel_id"]) if data["log_channel_id"] else discord.utils.get(guild.text_channels, name="logs")
    if isinstance(ch, discord.TextChannel):
        try:
            await ch.send(f"`[{datetime.now(timezone.utc).strftime('%H:%M:%S')}]` {text[:1800]}")
        except discord.HTTPException:
            pass

async def ensure_voice_channel():
    """دخول الروم الصوتي المحدد والبقاء فيه 24 ساعة مع إعادة الاتصال تلقائياً"""
    if not VOICE_CHANNEL_ID:
        return
    channel = bot.get_channel(VOICE_CHANNEL_ID)
    if not isinstance(channel, discord.VoiceChannel):
        return

    guild = channel.guild
    async with bot.voice_lock:
        vc = guild.voice_client
        try:
            if vc and vc.is_connected():
                if vc.channel != channel:
                    await vc.move_to(channel)
                return

            await channel.connect(reconnect=True, self_deaf=True)
        except Exception as e:
            await diag_log(guild, f"VOICE ERROR | ch={channel.id} | err={e}")

async def get_actor(guild: discord.Guild, action: discord.AuditLogAction, target_id: int | None = None, window: float = 5.0):
    if not guild.me or not guild.me.guild_permissions.view_audit_log:
        return None
    try:
        async for entry in guild.audit_logs(limit=4, action=action):
            if (discord.utils.utcnow() - entry.created_at).total_seconds() <= window:
                if target_id is None or getattr(entry.target, "id", None) == target_id:
                    return entry.user
    except (discord.Forbidden, discord.HTTPException):
        return None
    return None

async def strip_roles_only(guild: discord.Guild, member: discord.Member, reason: str, timeout_min: int = 30):
    """سحب الرتب فقط بدون طرد أو باند"""
    if not member or member.bot or member.id == guild.owner_id or not guild.me:
        return
    removable = [
        r for r in member.roles
        if r != guild.default_role and not r.managed and r.position < guild.me.top_role.position
    ]
    if removable:
        try:
            await member.remove_roles(*removable, reason=f"[Fox Guard] {reason}")
        except discord.HTTPException:
            pass
    if timeout_min > 0 and member.top_role.position < guild.me.top_role.position:
        try:
            await member.timeout(discord.utils.utcnow() + timedelta(minutes=timeout_min), reason=f"[Fox Guard] {reason}")
        except discord.HTTPException:
            pass

async def enforce_defense(
    guild: discord.Guild,
    actor: discord.abc.User | None,
    module_key: str,
    reason: str,
    critical: bool = False,
    trigger_panic_on_multi: bool = False,
):
    if not actor or await is_trusted(guild, actor):
        return False

    data = await bot.cfg(guild.id)
    mods = data["modules"]
    if not mods.get("anti_nuke", True) or not mods.get(module_key, True):
        return False

    lock = bot.actor_locks[(guild.id, actor.id)]
    async with lock:
        now = time.time()
        bucket = (guild.id, actor.id, module_key)
        bot.nuke_hits[bucket] = [t for t in bot.nuke_hits[bucket] if now - t < 15]
        bot.nuke_hits[bucket].append(now)

        total_recent = sum(
            1 for (gid, uid, _), ts_list in bot.nuke_hits.items()
            if gid == guild.id and uid == actor.id and any(now - x < 10 for x in ts_list)
        )

        if not critical and len(bot.nuke_hits[bucket]) < data["nuke_limit"] and total_recent < 3:
            return False

        mark = (guild.id, actor.id)
        if mark in bot.recent_punished:
            return True
        bot.recent_punished.add(mark)
        bot.loop.call_later(8, lambda: bot.recent_punished.discard(mark))

        member = guild.get_member(actor.id)
        if member:
            await strip_roles_only(guild, member, reason, data.get("timeout_minutes", 30))

        if trigger_panic_on_multi and total_recent >= 3 and not data["panic"]:
            await activate_panic(guild, data, reason=f"هجوم متزامن من {actor.id}")

        await diag_log(guild, f"لاتخرب السيرفر ياقحبه | <@{actor.id}> (`{actor.id}`) | {reason}")
        return True

def _has_dangerous_perms(perms: discord.Permissions) -> bool:
    return any([
        perms.administrator,
        perms.manage_guild,
        perms.ban_members,
        perms.manage_roles,
        perms.manage_channels,
    ])

def snapshot_guild(guild: discord.Guild) -> dict:
    roles = [
        {
            "name": r.name,
            "permissions": r.permissions.value,
            "color": r.color.value,
            "hoist": r.hoist,
            "mentionable": r.mentionable,
            "position": r.position,
        }
        for r in sorted(guild.roles, key=lambda x: x.position)
        if not r.is_default() and not r.managed
    ]
    categories = [{"name": c.name, "position": c.position} for c in sorted(guild.categories, key=lambda x: x.position)]
    channels = [
        {
            "name": c.name,
            "type": "voice" if isinstance(c, discord.VoiceChannel) else "text",
            "category": c.category.name if c.category else None,
            "position": c.position,
            "topic": getattr(c, "topic", None),
            "slowmode": getattr(c, "slowmode_delay", 0),
        }
        for c in sorted(guild.channels, key=lambda x: x.position)
        if not isinstance(c, discord.CategoryChannel)
    ]
    return {
        "name": guild.name,
        "verification_level": guild.verification_level.value,
        "roles": roles,
        "categories": categories,
        "channels": channels,
    }

async def apply_guild_restore(guild: discord.Guild, snap: dict):
    role_map = {r.name: r for r in guild.roles}
    for r_data in snap.get("roles", []):
        r_obj = role_map.get(r_data["name"])
        perms = discord.Permissions(r_data["permissions"])
        color = discord.Color(r_data["color"])
        if not r_obj:
            try:
                created = await guild.create_role(
                    name=r_data["name"],
                    permissions=perms,
                    color=color,
                    hoist=r_data.get("hoist", False),
                    mentionable=r_data.get("mentionable", False),
                    reason="[Fox Guard] استعادة رتبة",
                )
                role_map[created.name] = created
            except discord.HTTPException:
                pass
        else:
            try:
                await r_obj.edit(
                    permissions=perms,
                    color=color,
                    hoist=r_data.get("hoist", r_obj.hoist),
                    mentionable=r_data.get("mentionable", r_obj.mentionable),
                    reason="[Fox Guard] استعادة رتبة",
                )
            except discord.HTTPException:
                pass

    cat_map = {c.name: c for c in guild.categories}
    for c_data in snap.get("categories", []):
        if c_data["name"] not in cat_map:
            try:
                created_cat = await guild.create_category(name=c_data["name"], reason="[Fox Guard] استعادة تصنيف")
                cat_map[created_cat.name] = created_cat
            except discord.HTTPException:
                pass

    existing_ch = {c.name for c in guild.channels if not isinstance(c, discord.CategoryChannel)}
    for ch_data in snap.get("channels", []):
        if ch_data["name"] not in existing_ch:
            parent = cat_map.get(ch_data.get("category"))
            try:
                if ch_data["type"] == "voice":
                    await guild.create_voice_channel(name=ch_data["name"], category=parent, reason="[Fox Guard] استعادة روم صوتي")
                else:
                    await guild.create_text_channel(
                        name=ch_data["name"],
                        category=parent,
                        topic=ch_data.get("topic"),
                        slowmode_delay=ch_data.get("slowmode", 0),
                        reason="[Fox Guard] استعادة روم نصي",
                    )
            except discord.HTTPException:
                pass

# ==========================================
# 5. أحداث الحماية والبقاء الصوتي
# ==========================================

@bot.event
async def on_ready():
    print(f"✅ Fox Guard شغال: {bot.user}")
    await bot.change_presence(activity=discord.Game(name="انا من صنع العم فوكس"))
    await asyncio.sleep(3)
    await ensure_voice_channel()

@bot.event
async def on_voice_state_update(member, before, after):
    if bot.user and member.id == bot.user.id:
        if before.channel and after.channel is None:
            await asyncio.sleep(2)
            await ensure_voice_channel()
        elif after.channel and after.channel.id != VOICE_CHANNEL_ID:
            await asyncio.sleep(2)
            await ensure_voice_channel()
        return

    if before.channel and after.channel is None:
        actor = await get_actor(member.guild, discord.AuditLogAction.member_disconnect, member.id, window=4.0)
        await enforce_defense(member.guild, actor, "anti_nuke", "فصل جماعي من الصوت")

@bot.event
async def on_guild_channel_delete(channel):
    guild = channel.guild
    actor = await get_actor(guild, discord.AuditLogAction.channel_delete, channel.id)
    if await enforce_defense(guild, actor, "anti_channel", "حذف روم بدون وايت ليست", critical=True, trigger_panic_on_multi=True):
        try:
            await channel.clone(reason="[Fox Guard] استعادة روم محذوف")
        except discord.HTTPException:
            pass

@bot.event
async def on_guild_channel_create(channel):
    actor = await get_actor(channel.guild, discord.AuditLogAction.channel_create, channel.id)
    await enforce_defense(channel.guild, actor, "anti_channel", "إنشاء رومات عشوائي/جماعي", trigger_panic_on_multi=True)

@bot.event
async def on_guild_channel_update(before, after):
    actor = await get_actor(after.guild, discord.AuditLogAction.channel_update, after.id)
    await enforce_defense(after.guild, actor, "anti_channel", "تعديل روم بدون وايت ليست")

@bot.event
async def on_member_update(before, after):
    if before.roles == after.roles:
        return
    added = [r for r in after.roles if r not in before.roles]
    admin_added = [r for r in added if r.permissions.administrator]
    if not admin_added:
        return

    actor = await get_actor(after.guild, discord.AuditLogAction.member_role_update, after.id)
    if actor and not await is_trusted(after.guild, actor):
        try:
            await after.remove_roles(*admin_added, reason="[Fox Guard] منع إعطاء Administrator بدون وايت ليست")
        except discord.HTTPException:
            pass
        await enforce_defense(after.guild, actor, "anti_admin", "إعطاء رتبة Administrator بدون وايت ليست", critical=True, trigger_panic_on_multi=True)

@bot.event
async def on_guild_role_delete(role):
    guild = role.guild
    data = await bot.cfg(guild.id)
    actor = await get_actor(guild, discord.AuditLogAction.role_delete, role.id)
    is_crit = role.id in data["protected_roles"] or role.permissions.administrator
    if await enforce_defense(guild, actor, "anti_role", "حذف رتبة بدون وايت ليست", critical=is_crit, trigger_panic_on_multi=True):
        try:
            await guild.create_role(
                name=role.name,
                permissions=role.permissions,
                color=role.color,
                hoist=role.hoist,
                mentionable=role.mentionable,
                reason="[Fox Guard] استعادة رتبة محذوفة",
            )
        except discord.HTTPException:
            pass

@bot.event
async def on_guild_role_update(before, after):
    guild = after.guild
    data = await bot.cfg(guild.id)
    actor = await get_actor(guild, discord.AuditLogAction.role_update, after.id)
    escalated = (not _has_dangerous_perms(before.permissions)) and _has_dangerous_perms(after.permissions)
    if escalated and actor and not await is_trusted(guild, actor):
        try:
            await after.edit(permissions=before.permissions, reason="[Fox Guard] منع تصعيد صلاحيات رتبة")
        except discord.HTTPException:
            pass
        await enforce_defense(guild, actor, "anti_admin", "تصعيد صلاحيات خطيرة لرتبة", critical=True, trigger_panic_on_multi=True)
    elif after.id in data["protected_roles"]:
        await enforce_defense(guild, actor, "anti_role", "تعديل رتبة محمية", critical=True)

@bot.event
async def on_member_ban(guild, user):
    actor = await get_actor(guild, discord.AuditLogAction.ban, user.id)
    await enforce_defense(guild, actor, "anti_admin", "محاولة حظر بدون وايت ليست", critical=True, trigger_panic_on_multi=True)

@bot.event
async def on_member_remove(member):
    actor = await get_actor(member.guild, discord.AuditLogAction.kick, member.id, window=4.0)
    if actor:
        await enforce_defense(member.guild, actor, "anti_admin", "محاولة طرد بدون وايت ليست", critical=True, trigger_panic_on_multi=True)

@bot.event
async def on_webhooks_update(channel):
    guild = channel.guild
    actor = await get_actor(guild, discord.AuditLogAction.webhook_create)
    if actor and not await is_trusted(guild, actor):
        try:
            for wh in await channel.webhooks():
                if wh.user and wh.user.id == actor.id:
                    await wh.delete(reason="[Fox Guard] حذف ويب هوك غير مصرح")
        except discord.HTTPException:
            pass
        await enforce_defense(guild, actor, "anti_nuke", "إنشاء ويب هوك غير مصرح", critical=True)

@bot.event
async def on_member_join(member):
    guild = member.guild
    data = await bot.cfg(guild.id)

    if member.bot:
        if member.id not in data["trusted_bots"]:
            actor = await get_actor(guild, discord.AuditLogAction.bot_add, member.id)
            if actor and not await is_trusted(guild, actor):
                await enforce_defense(guild, actor, "anti_bot", f"إضافة بوت غير مصرح ({member.name})", critical=True)
        return

    if data["modules"].get("anti_raid", True):
        now = time.time()
        bot.joins[guild.id] = [t for t in bot.joins[guild.id] if now - t < data["raid_window"]]
        bot.joins[guild.id].append(now)
        if len(bot.joins[guild.id]) >= data["raid_joins"] and not data["panic"]:
            await activate_panic(guild, data, reason="دخول جماعي سريع (Anti-Raid)")

# ==========================================
# 6. الرياكشن التلقائي + الذكاء الاصطناعي + Anti-Spam + Anti-Link
# ==========================================

LINK_REGEX = re.compile(
    r"(https?://|www\.|discord\.gg/|discord\.com/invite/|discordapp\.com/invite/)",
    re.IGNORECASE,
)

@bot.event
async def on_message(message):
    if message.author.bot or message.guild is None:
        await bot.process_commands(message)
        return

    # الرياكشن التلقائي
    if message.channel.id == REACTION_CHANNEL_ID:
        for idx, emoji in enumerate(get_reaction_emojis(message.guild), start=1):
            try:
                await message.add_reaction(emoji)
                await asyncio.sleep(0.3)
            except discord.HTTPException as err:
                print(f"❌ خطأ الرياكشن رقم {idx} ({emoji}) | status={err.status} | code={err.code}: {err.text}")

    # الرد بالذكاء الاصطناعي عند منشن البوت أو الرد على رسالته
    is_mentioned = bot.user in message.mentions
    is_reply_to_bot = (
        message.reference
        and isinstance(message.reference.resolved, discord.Message)
        and message.reference.resolved.author.id == bot.user.id
    )

    if is_mentioned or is_reply_to_bot:
        is_father = OWNER_FATHER_ID != 0 and message.author.id == OWNER_FATHER_ID
        clean_text = message.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()
        if not clean_text:
            clean_text = "سم يا بابا؟" if is_father else "وش تبي؟"
        async with message.channel.typing():
            reply_text = await generate_rough_reply(message.author.display_name, clean_text, is_father)
        await message.reply(reply_text, mention_author=True)
        return

    data = await bot.cfg(message.guild.id)
    mods = data["modules"]

    if await is_trusted(message.guild, message.author) or message.channel.id in data["ignored_channels"]:
        await bot.process_commands(message)
        return

    if mods.get("anti_links", True) and LINK_REGEX.search(message.content) and not message.author.guild_permissions.administrator:
        try:
            await message.delete()
            await message.channel.send(f"⚠️ {message.author.mention}، يمنع إرسال الروابط أو الدعوات هنا!", delete_after=5)
        except discord.HTTPException:
            pass
        return

    if mods.get("anti_spam", True):
        now = time.time()
        ukey = (message.guild.id, message.author.id)
        bot.spam[ukey] = [t for t in bot.spam[ukey] if now - t < data["spam_window"]]
        bot.spam[ukey].append(now)
        mass_mention = len(message.mentions) >= 5 or message.mention_everyone
        if len(bot.spam[ukey]) >= data["spam_msgs"] or mass_mention:
            try:
                await message.delete()
                await message.channel.send(f"⚠️ {message.author.mention}، توقف عن التكرار أو المنشن الجماعي!", delete_after=5)
                if mass_mention and message.author.top_role.position < message.guild.me.top_role.position:
                    await message.author.timeout(discord.utils.utcnow() + timedelta(minutes=10), reason="[Fox Guard] منشن جماعي")
            except discord.HTTPException:
                pass
            return

    await bot.process_commands(message)

# ==========================================
# 7. Panic Mode + Trust System + Backup/Restore
# ==========================================

async def activate_panic(guild: discord.Guild, data: dict, reason: str = "Manual Panic"):
    if data.get("panic_prev_perms") is None:
        data["panic_prev_perms"] = guild.default_role.permissions.value
    data["panic"] = True
    await bot.set_cfg(guild.id, data)

    locked_perms = discord.Permissions(data["panic_prev_perms"])
    locked_perms.update(send_messages=False)
    try:
        await guild.default_role.edit(permissions=locked_perms, reason=f"[Fox Guard] {reason}")
    except discord.HTTPException:
        pass
    await diag_log(guild, f"PANIC ON | reason={reason}")

async def deactivate_panic(guild: discord.Guild, data: dict):
    prev = data.get("panic_prev_perms")
    if prev is not None:
        try:
            await guild.default_role.edit(permissions=discord.Permissions(prev), reason="[Fox Guard] استرجاع صلاحيات @everyone")
        except discord.HTTPException:
            pass
    data["panic"] = False
    data["panic_prev_perms"] = None
    await bot.set_cfg(guild.id, data)
    await diag_log(guild, "PANIC OFF | restored @everyone permissions")

def trust_guard():
    async def pred(ctx):
        if not ctx.guild:
            return False
        if await is_trusted(ctx.guild, ctx.author):
            return True
        await ctx.reply("❌ هذا الأمر متاح للمالك أو الموثوقين فقط.")
        return False
    return commands.check(pred)

@bot.command(name="panic")
@trust_guard()
async def panic_cmd(ctx):
    data = await bot.cfg(ctx.guild.id, force_reload=True)
    if not data["panic"]:
        await activate_panic(ctx.guild, data, reason=f"تفعيل بواسطة {ctx.author.id}")
        await ctx.reply("🔒 تم تفعيل **وضع الطوارئ** وحفظ الصلاحيات السابقة.")
    else:
        await deactivate_panic(ctx.guild, data)
        await ctx.reply("🔓 تم إيقاف **وضع الطوارئ** واسترجاع الصلاحيات السابقة.")

@bot.command(name="trust")
@trust_guard()
async def trust_cmd(ctx, kind: str, action: str, target: str):
    data = await bot.cfg(ctx.guild.id, force_reload=True)
    kind_map = {
        "user": "trusted_users",
        "bot": "trusted_bots",
        "role": "protected_roles",
        "channel": "ignored_channels",
    }
    key = kind_map.get(kind.lower())
    if not key:
        return await ctx.reply("❌ استخدم: `user` أو `bot` أو `role` أو `channel`")

    digits = re.sub(r"\D", "", target)
    if not digits:
        return await ctx.reply("❌ أدخل ID أو منشن صحيح.")
    tid = int(digits)

    if action.lower() in ("add", "+", "اضافة"):
        if tid not in data[key]:
            data[key].append(tid)
            await bot.set_cfg(ctx.guild.id, data)
        await ctx.reply(f"✅ أُضيف `{tid}` إلى `{key}`.")
    elif action.lower() in ("remove", "-", "حذف"):
        if tid in data[key]:
            data[key].remove(tid)
            await bot.set_cfg(ctx.guild.id, data)
        await ctx.reply(f"🗑️ أُزيل `{tid}` من `{key}`.")

@bot.command(name="backup")
@trust_guard()
async def backup_cmd(ctx):
    bid = await bot.db.save_backup(ctx.guild.id, snapshot_guild(ctx.guild))
    await ctx.reply(f"💾 تم حفظ النسخة الاحتياطية `#{bid}`.")

@bot.command(name="restore")
@trust_guard()
async def restore_cmd(ctx, backup_id: int | None = None):
    snap = await bot.db.get_backup(ctx.guild.id, backup_id)
    if not snap:
        return await ctx.reply("❌ لا توجد نسخة احتياطية.")
    await apply_guild_restore(ctx.guild, snap)
    await ctx.reply("♻️ تمت الاستعادة.")

@bot.command(name="toggle")
@trust_guard()
async def toggle_cmd(ctx, mod_name: str, state: str):
    data = await bot.cfg(ctx.guild.id, force_reload=True)
    if mod_name not in data["modules"]:
        return await ctx.reply(f"❌ الوحدات المتاحة: `{', '.join(data['modules'].keys())}`")
    data["modules"][mod_name] = state.lower() in ("on", "true", "1", "تشغيل")
    await bot.set_cfg(ctx.guild.id, data)
    await ctx.reply(f"⚙️ `{mod_name}` = `{'ON' if data['modules'][mod_name] else 'OFF'}`")

# ==========================================
# 8. أوامر الإرسال والبرودكاست (سوالف البوت + منشن المستلم)
# ==========================================

@bot.command(name="say")
@commands.has_permissions(administrator=True)
async def say_to_channel(ctx, channel_id: int, *, message: str = None):
    if not message:
        await ctx.reply("❌ اكتب آيدي الروم وبعده الكلام اللي تبي البوت يكتبه.")
        return

    target_channel = bot.get_channel(channel_id) or ctx.guild.get_channel(channel_id)
    if not isinstance(target_channel, discord.abc.Messageable):
        await ctx.reply("❌ تأكد من آيدي الروم أو إن البوت يقدر يرسل فيه.")
        return

    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass

    try:
        await target_channel.send(message)
    except discord.HTTPException as err:
        await ctx.reply(f"❌ ما قدرت أرسل للروم: `{err}`")

@bot.command(name="bc", aliases=["broadcast"])
@commands.has_permissions(administrator=True)
async def broadcast(ctx, *, msg: str = None):
    if not msg:
        await ctx.reply("❌ يرجى كتابة الرسالة المراد إرسالها للأعضاء!")
        return

    status_msg = await ctx.send("⏳ جاري إرسال البرودكاست للأعضاء...")
    success_count = 0
    fail_count = 0

    for member in ctx.guild.members:
        if member.bot:
            continue
        formatted_msg = f"{msg}\n\n{member.mention}"
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

@bot.command(name="bca", aliases=["adminbc"])
@commands.has_permissions(administrator=True)
async def admin_broadcast(ctx, *, msg: str = None):
    if not msg:
        await ctx.reply("❌ يرجى كتابة الرسالة المراد إرسالها للإداريين فقط!")
        return

    status_msg = await ctx.send("⏳ جاري إرسال البرودكاست للإداريين فقط...")
    success_count = 0
    fail_count = 0

    for member in ctx.guild.members:
        if member.bot or not member.guild_permissions.administrator:
            continue
        formatted_msg = f"{msg}\n\n{member.mention}"
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

@say_to_channel.error
@broadcast.error
@admin_broadcast.error
async def broadcast_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.reply("❌ لا تمتلك صلاحية استخدام هذا الأمر!")
    elif isinstance(error, commands.BadArgument):
        await ctx.reply("❌ الصيغة الصحيحة: `!say <آيدي_الروم> <الكلام>`")

# ==========================================
# 9. تشغيل البوت
# ==========================================

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("❌ خطأ: لم يتم العثور على التوكن في متغيرات البيئة (DISCORD_TOKEN)!")
    else:
        bot.run(DISCORD_TOKEN)
