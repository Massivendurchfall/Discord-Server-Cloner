import asyncio
import aiohttp
import discord
import datetime
from discord import PermissionOverwrite
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
            retry_after = e.response.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = float(retry_after)
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
            return await r.read()
        return None

def build_overwrites(src_overwrites, role_map, target_guild):
    result = {}
    for target, ow in src_overwrites.items():
        if isinstance(target, discord.Role):
            if target.is_default():
                result[target_guild.default_role] = PermissionOverwrite(**{k: v for k, v in dict(ow).items() if v is not None})
            else:
                mapped = role_map.get(target.id)
                if mapped:
                    result[mapped] = PermissionOverwrite(**{k: v for k, v in dict(ow).items() if v is not None})
    return result

async def clear_target_guild(guild):
    log("Clearing target guild...", Fore.MAGENTA)
    for c in guild.channels:
        await safe_call(c.delete(), f"delete channel {c.name}")
    for r in guild.roles:
        if not r.is_default():
            await safe_call(r.delete(), f"delete role {r.name}")
    for e in guild.emojis:
        await safe_call(e.delete(), f"delete emoji {e.name}")
    log("Target guild cleared.", Fore.GREEN)

async def clone_roles(source, target):
    mapping = {}
    roles = [r for r in source.roles if not r.is_default()]
    roles_sorted = sorted(roles, key=lambda r: r.position, reverse=True)  # highest first
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
        await asyncio.sleep(0.4)

    # reorder to match source order exactly
    try:
        items = []
        for src_id, new_role in mapping.items():
            src_role = source.get_role(src_id)
            if src_role:
                items.append((new_role, src_role.position))
        items_sorted = sorted(items, key=lambda x: x[1])
        positions = {role: pos for role, (_, pos) in zip([i[0] for i in items_sorted], items_sorted)}
        await target.edit_role_positions(positions={i[0]: i[1] for i in items_sorted})
    except Exception as e:
        log(f"Failed to set role order: {e}", Fore.RED)
    return mapping

async def clone_categories_and_channels(source, target, role_map):
    cat_map = {}
    for cat in sorted(source.categories, key=lambda c: c.position):
        ow = build_overwrites(cat.overwrites, role_map, target)
        new_cat = await safe_call(target.create_category(name=cat.name, overwrites=ow, position=cat.position), f"category {cat.name}")
        if new_cat:
            cat_map[cat.id] = new_cat
            log(f"Created category: {cat.name}", Fore.BLUE)
        await asyncio.sleep(0.4)

    for ch in sorted(source.channels, key=lambda c: c.position):
        parent = ch.category
        new_parent = cat_map.get(parent.id) if parent else None
        ow = build_overwrites(ch.overwrites, role_map, target)
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
            nc = await safe_call(target.create_voice_channel(
                name=ch.name,
                overwrites=ow,
                category=new_parent,
                position=ch.position,
                bitrate=ch.bitrate,
                user_limit=ch.user_limit
            ), f"voice channel {ch.name}")
        elif isinstance(ch, discord.StageChannel):
            nc = await safe_call(target.create_stage_channel(
                name=ch.name,
                overwrites=ow,
                category=new_parent,
                position=ch.position
            ), f"stage channel {ch.name}")
        else:
            continue
        if nc:
            log(f"Created channel: {ch.name}", Fore.CYAN)
        await asyncio.sleep(0.4)

async def clone_emojis_and_stickers(source, target, session):
    emojis_sorted = sorted(source.emojis, key=lambda e: e.name)
    for emoji in emojis_sorted:
        data = await fetch_bytes(session, str(emoji.url))
        if data:
            await safe_call(target.create_custom_emoji(name=emoji.name, image=data), f"emoji {emoji.name}")
            log(f"Copied emoji: {emoji.name}", Fore.YELLOW)
        await asyncio.sleep(0.5)
    for sticker in source.stickers:
        if sticker.format != discord.StickerFormatType.png:
            continue
        data = await fetch_bytes(session, str(sticker.url))
        if data:
            await safe_call(target.create_sticker(name=sticker.name, description=sticker.description or "", emoji=sticker.emoji, file=discord.File(fp=data, filename=f"{sticker.name}.png")), f"sticker {sticker.name}")
            log(f"Copied sticker: {sticker.name}", Fore.YELLOW)
        await asyncio.sleep(0.6)

async def clone_webhooks(source, target):
    try:
        webhooks = await source.webhooks()
    except Exception:
        webhooks = []
    for wh in webhooks:
        if isinstance(wh.channel, discord.TextChannel):
            avatar_bytes = await wh.avatar.read() if wh.avatar else None
            await safe_call(wh.channel.create_webhook(name=wh.name, avatar=avatar_bytes), f"webhook {wh.name}")
            log(f"Copied webhook: {wh.name}", Fore.MAGENTA)
        await asyncio.sleep(0.5)

async def clone_guild_settings(source, target, session):
    data = await fetch_bytes(session, str(source.icon.url)) if source.icon else None
    splash = await fetch_bytes(session, str(source.splash.url)) if source.splash else None
    banner = await fetch_bytes(session, str(source.banner.url)) if source.banner else None
    kwargs = {
        "name": source.name,
        "description": source.description,
        "verification_level": source.verification_level,
        "explicit_content_filter": source.explicit_content_filter,
        "afk_timeout": source.afk_timeout,
        "preferred_locale": source.preferred_locale,
        "system_channel_flags": source.system_channel_flags,
    }
    if data: kwargs["icon"] = data
    if splash: kwargs["splash"] = splash
    if banner: kwargs["banner"] = banner
    await safe_call(target.edit(**kwargs), "guild settings")
    log(f"Updated guild settings: {source.name}", Fore.MAGENTA)

async def clone_guild(source, target, opts):
    if opts["clear_target"]:
        await clear_target_guild(target)

    role_map = {}
    async with aiohttp.ClientSession() as session:
        if opts["copy_roles"]:
            log("Cloning roles...", Fore.CYAN)
            role_map = await clone_roles(source, target)
        if opts["copy_channels"]:
            log("Cloning categories and channels...", Fore.CYAN)
            await clone_categories_and_channels(source, target, role_map)
        if opts["copy_emojis"]:
            log("Cloning emojis and stickers...", Fore.CYAN)
            await clone_emojis_and_stickers(source, target, session)
        if opts["copy_info"]:
            log("Cloning server settings (name, desc, icon, etc.)...", Fore.CYAN)
            await clone_guild_settings(source, target, session)
        if opts["copy_webhooks"]:
            log("Cloning webhooks...", Fore.CYAN)
            await clone_webhooks(source, target)
    log("Clone complete!", Fore.GREEN)

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
        "copy_info": ask_bool("Copy server name, description, icon, banner, etc.?", True)
    }


    client = discord.Client()

    @client.event
    async def on_ready():
        log(f"Logged in as {client.user}", Fore.GREEN)
        source = client.get_guild(source_id)
        target = client.get_guild(target_id)
        if not source or not target:
            log("Source or target guild not found. Ensure the bot is in both servers with admin permissions.", Fore.RED)
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
