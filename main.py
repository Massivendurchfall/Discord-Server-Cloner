import asyncio
import aiohttp
import discord
import datetime
import io
from discord import PermissionOverwrite, File
from colorama import Fore, Style, init

init(autoreset=True)

def log(msg, color=Fore.WHITE):
    t = datetime.datetime.now().strftime("%H:%M:%S")
    print(color + f"[{t}] {msg}")

def ask_bool(msg, default=False):
    d = "Y/n" if default else "y/N"
    while True:
        v = input(Fore.CYAN + f"{msg} ({d}): ").strip().lower()
        if not v:
            return default
        if v in ("y","yes"): return True
        if v in ("n","no"): return False

async def safe_call(coro, name, wait_on_rate=True):
    try:
        return await coro
    except discord.errors.HTTPException as e:
        if e.status == 429 and wait_on_rate and hasattr(e, "response") and e.response:
            ra = e.response.headers.get("Retry-After") or e.response.headers.get("retry-after")
            if ra:
                try:
                    delay = float(ra)
                    log(f"Rate limited on {name}, waiting {delay:.1f}s...", Fore.YELLOW)
                    await asyncio.sleep(delay)
                    return await safe_call(coro, name, wait_on_rate=False)
                except Exception:
                    pass
        log(f"HTTP Error on {name}: {e}", Fore.RED)
    except Exception as e:
        log(f"Error on {name}: {e}", Fore.RED)
    return None

async def fetch_bytes(session, url):
    async with session.get(url) as r:
        if r.status == 200:
            return await r.read(), (r.headers.get("Content-Type",""))
        return None, ""

def build_overwrites(src_overwrites, role_map, target_guild):
    result = {}
    for target, ow in src_overwrites.items():
        if isinstance(target, discord.Role):
            if target.is_default():
                result[target_guild.default_role] = PermissionOverwrite(**{k:v for k,v in dict(ow).items() if v is not None})
            else:
                mapped = role_map.get(target.id)
                if mapped:
                    result[mapped] = PermissionOverwrite(**{k:v for k,v in dict(ow).items() if v is not None})
    return result

async def clear_target_guild(guild):
    log("Clearing target guild...", Fore.MAGENTA)
    for c in list(guild.channels):
        await safe_call(c.delete(), f"delete channel {getattr(c,'name','?')}")
    for r in list(guild.roles):
        if not r.is_default():
            await safe_call(r.delete(), f"delete role {r.name}")
    for e in list(guild.emojis):
        await safe_call(e.delete(), f"delete emoji {e.name}")
    try:
        stickers = await guild.stickers()
    except Exception:
        stickers = []
    for s in stickers:
        await safe_call(s.delete(), f"delete sticker {s.name}")
    log("Target guild cleared.", Fore.GREEN)

async def clone_roles(source, target):
    mapping = {}
    roles = [r for r in source.roles if not r.is_default()]
    roles_sorted = sorted(roles, key=lambda r: r.position, reverse=True)
    for r in roles_sorted:
        if r.managed:
            continue
        newr = await safe_call(target.create_role(
            name=r.name,
            permissions=r.permissions,
            colour=r.colour,
            hoist=r.hoist,
            mentionable=r.mentionable
        ), f"role {r.name}")
        if newr:
            mapping[r.id] = newr
            log(f"Created role: {r.name}", Fore.GREEN)
        await asyncio.sleep(0.25)
    try:
        items = []
        for src_id, new_role in mapping.items():
            src_role = source.get_role(src_id)
            if src_role:
                items.append((new_role, src_role.position))
        items_sorted = sorted(items, key=lambda x: x[1])
        await target.edit_role_positions(positions={i[0]: i[1] for i in items_sorted})
    except Exception as e:
        log(f"Failed to set role order: {e}", Fore.RED)
    return mapping

def max_bitrate_for_tier(tier):
    if tier <= 0: return 96000
    if tier == 1: return 128000
    if tier == 2: return 256000
    return 384000

async def clone_categories_and_channels(source, target, role_map, summary):
    cat_map = {}
    for cat in sorted(source.categories, key=lambda c: c.position):
        ow = build_overwrites(cat.overwrites, role_map, target)
        new_cat = await safe_call(target.create_category(name=cat.name, overwrites=ow, position=cat.position), f"category {cat.name}")
        if new_cat:
            cat_map[cat.id] = new_cat
            log(f"Created category: {cat.name}", Fore.BLUE)
            summary["categories"] += 1
        await asyncio.sleep(0.25)
    tier = target.premium_tier
    limit = max_bitrate_for_tier(tier)
    channel_map = {}
    for ch in sorted(source.channels, key=lambda c: c.position):
        parent = ch.category
        new_parent = cat_map.get(parent.id) if parent else None
        ow = build_overwrites(ch.overwrites, role_map, target)
        nc = None
        if isinstance(ch, discord.TextChannel):
            nc = await safe_call(target.create_text_channel(
                name=ch.name,
                topic=ch.topic or "",
                slowmode_delay=ch.slowmode_delay,
                nsfw=ch.nsfw,
                overwrites=ow,
                category=new_parent,
                position=ch.position
            ), f"text channel {ch.name}")
        elif isinstance(ch, discord.VoiceChannel):
            bitrate = min(ch.bitrate, limit)
            nc = await safe_call(target.create_voice_channel(
                name=ch.name,
                overwrites=ow,
                category=new_parent,
                position=ch.position,
                bitrate=bitrate,
                user_limit=ch.user_limit
            ), f"voice channel {ch.name}")
        elif isinstance(ch, discord.StageChannel):
            nc = await safe_call(target.create_stage_channel(
                name=ch.name,
                overwrites=ow,
                category=new_parent,
                position=ch.position
            ), f"stage channel {ch.name}")
        if nc:
            channel_map[ch.id] = nc
            log(f"Created channel: {ch.name}", Fore.CYAN)
            summary["channels"] += 1
        await asyncio.sleep(0.25)
    return channel_map

def is_supported_image(content_type):
    if not content_type: return False
    ct = content_type.lower()
    return ("png" in ct) or ("jpeg" in ct) or ("jpg" in ct)

async def clone_guild_settings(source, target, session, channel_map):
    icon_bytes = None
    splash_bytes = None
    banner_bytes = None
    if source.icon:
        data, ct = await fetch_bytes(session, str(source.icon.url))
        if data and is_supported_image(ct): icon_bytes = data
        else: log(f"Skipped unsupported icon type: {ct}", Fore.YELLOW)
    if source.splash:
        data, ct = await fetch_bytes(session, str(source.splash.url))
        if data and is_supported_image(ct): splash_bytes = data
        else: log(f"Skipped unsupported splash type: {ct}", Fore.YELLOW)
    if source.banner:
        data, ct = await fetch_bytes(session, str(source.banner.url))
        if data and is_supported_image(ct): banner_bytes = data
        else: log(f"Skipped unsupported banner type: {ct}", Fore.YELLOW)
    kwargs = {
        "name": source.name,
        "description": source.description,
        "verification_level": source.verification_level,
        "explicit_content_filter": source.explicit_content_filter,
        "afk_timeout": source.afk_timeout,
        "preferred_locale": source.preferred_locale,
        "system_channel_flags": source.system_channel_flags,
        "default_notifications": source.default_notifications
    }
    if icon_bytes: kwargs["icon"] = icon_bytes
    if splash_bytes: kwargs["splash"] = splash_bytes
    if banner_bytes: kwargs["banner"] = banner_bytes
    if source.afk_channel and source.afk_channel.id in channel_map:
        kwargs["afk_channel"] = channel_map[source.afk_channel.id]
    if source.system_channel and source.system_channel.id in channel_map:
        kwargs["system_channel"] = channel_map[source.system_channel.id]
    if getattr(source, "rules_channel", None) and source.rules_channel.id in channel_map:
        kwargs["rules_channel"] = channel_map[source.rules_channel.id]
    if getattr(source, "public_updates_channel", None) and source.public_updates_channel.id in channel_map:
        kwargs["public_updates_channel"] = channel_map[source.public_updates_channel.id]
    ok = await safe_call(target.edit(**kwargs), "guild settings")
    if ok:
        log("Updated guild settings and visuals.", Fore.MAGENTA)
    else:
        log("Failed to update some guild settings or visuals.", Fore.RED)

async def clone_emojis_and_stickers(source, target, session, summary):
    emojis_sorted = sorted(source.emojis, key=lambda e: (e.created_at or datetime.datetime.fromtimestamp(((e.id>>22)+1420070400000)/1000.0)))
    for emoji in emojis_sorted:
        data, ct = await fetch_bytes(session, str(emoji.url))
        if data:
            await safe_call(target.create_custom_emoji(name=emoji.name, image=data), f"emoji {emoji.name}")
            log(f"Copied emoji: {emoji.name}", Fore.YELLOW)
            summary["emojis"] += 1
        await asyncio.sleep(0.4)
    try:
        stickers = await source.stickers()
    except Exception:
        stickers = []
    for sticker in stickers:
        if sticker.format != discord.StickerFormatType.png:
            continue
        data, ct = await fetch_bytes(session, str(sticker.url))
        if data:
            f = File(io.BytesIO(data), filename=f"{sticker.name}.png")
            await safe_call(target.create_sticker(name=sticker.name, description=sticker.description or "", emoji=sticker.emoji, file=f), f"sticker {sticker.name}")
            log(f"Copied sticker: {sticker.name}", Fore.YELLOW)
            summary["stickers"] += 1
        await asyncio.sleep(0.5)

async def clone_webhooks(source, target, channel_map, summary):
    try:
        webhooks = await source.webhooks()
    except Exception:
        webhooks = []
    for wh in webhooks:
        tgt_ch = None
        if wh.channel and wh.channel.id in channel_map:
            tgt_ch = channel_map[wh.channel.id]
        if isinstance(tgt_ch, discord.TextChannel):
            avatar_bytes = None
            try:
                avatar_bytes = await wh.avatar.read() if wh.avatar else None
            except Exception:
                avatar_bytes = None
            await safe_call(tgt_ch.create_webhook(name=wh.name, avatar=avatar_bytes, reason="Cloned webhook"), f"webhook {wh.name}")
            log(f"Copied webhook: {wh.name}", Fore.MAGENTA)
            summary["webhooks"] += 1
        await asyncio.sleep(0.4)

async def clone_guild(source, target, opts):
    summary = {"roles": 0, "categories": 0, "channels": 0, "emojis": 0, "stickers": 0, "webhooks": 0}
    if opts["clear_target"]:
        await clear_target_guild(target)
    role_map = {}
    channel_map = {}
    async with aiohttp.ClientSession() as session:
        if opts["copy_roles"]:
            log("Cloning roles...", Fore.CYAN)
            role_map = await clone_roles(source, target)
            summary["roles"] = len(role_map)
        if opts["copy_channels"]:
            log("Cloning categories and channels...", Fore.CYAN)
            channel_map = await clone_categories_and_channels(source, target, role_map, summary)
        if opts["copy_emojis"]:
            log("Cloning emojis and stickers...", Fore.CYAN)
            await clone_emojis_and_stickers(source, target, session, summary)
        if opts["copy_info"]:
            log("Cloning server settings (name, description, visuals, system/AFK)...", Fore.CYAN)
            await clone_guild_settings(source, target, session, channel_map)
        if opts["copy_webhooks"]:
            log("Cloning webhooks...", Fore.CYAN)
            await clone_webhooks(source, target, channel_map, summary)
    log("Clone complete!", Fore.GREEN)
    log("========= SUMMARY =========", Fore.MAGENTA)
    for k, v in summary.items():
        log(f"{k.capitalize()}: {v}", Fore.CYAN)
    log("===========================", Fore.MAGENTA)

async def main():
    token = input(Fore.CYAN + "Enter bot token: ").strip()
    source_id = int(input(Fore.CYAN + "Enter source guild ID: ").strip())
    target_id = int(input(Fore.CYAN + "Enter target guild ID: ").strip())
    opts = {
        "clear_target": ask_bool("Delete everything in target guild before cloning?", True),
        "copy_roles": ask_bool("Copy roles?", True),
        "copy_channels": ask_bool("Copy categories and channels?", True),
        "copy_emojis": ask_bool("Copy emojis and stickers?", True),
        "copy_webhooks": ask_bool("Copy webhooks?", True),
        "copy_info": ask_bool("Copy server name, description, icon, banner, splash, AFK/system/rules/public updates, notifications?", True)
    }

    client = discord.Client()
    @client.event
    async def on_ready():
        log(f"Logged in as {client.user}", Fore.GREEN)
        source = client.get_guild(source_id)
        target = client.get_guild(target_id)
        if not source or not target:
            log("Source or target guild not found. Ensure the bot is in both servers with sufficient permissions.", Fore.RED)
            await client.close()
            return
        await clone_guild(source, target, opts)
        await client.close()
    await client.start(token)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("Aborted by user.", Fore.YELLOW)
