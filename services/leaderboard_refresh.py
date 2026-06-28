"""
Helper to automatically refresh the leaderboard after an ELO change.
Deletes the bot's last leaderboard message in the `#leaderboard` channel
then posts a new image generated from the current data in the database.

Used by:
  - cogs/match.py (after applying post-match ELO)
  - bot.py /elomodify, /resetelo (slash + prefix)
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from datetime import UTC, datetime
from io import BytesIO

import discord

from leaderboard_img import generate_leaderboard
from services import repository
from services.inactivity import is_active

logger = logging.getLogger(__name__)


LEADERBOARD_CHANNEL_NAME = "leaderboard"
LEADERBOARD_FILENAME = "leaderboard.png"
PAGE_SIZE = 15


# Per-guild debounce: avoid regenerating + reposting the leaderboard
# in bursts after N consecutive ELO changes (e.g. /win + /lose + other
# admin ops). Discord rate-limits delete+send (~5/min/channel).
_REFRESH_DEBOUNCE_SECONDS: int = 30
# Bounds the cache to avoid a memory leak if the bot runs on many
# guilds (one entry per guild_id, never purged). LRU with FIFO
# eviction of the oldest access beyond _MAX_GUILDS_TRACKED.
_MAX_GUILDS_TRACKED: int = 1024
_LAST_REFRESH_AT: OrderedDict[tuple[int, str], datetime] = OrderedDict()

# -- Rendered page cache -------------------------------------------
# Lazy cache: the page is rendered on first lookup and stored as
# bytes (not as discord.File which is single-use). On every cache
# hit we wrap the bytes in a new BytesIO + File.
#
# Invalidation: `refresh_leaderboard_channel` clears all entries
# (guild_id, queue_type, *) before regenerating page 1. Every ELO
# mutation goes through this function -> consistency guaranteed
# as long as this contract is respected.
#
# Key : (guild_id, queue_type, page_zero_indexed)
# Val : (png_bytes, total_pages_at_render_time)
_PAGE_CACHE_MAXSIZE: int = 60  # ~3 queues * 20 pages
_PAGE_CACHE: OrderedDict[tuple[int, str, int], tuple[bytes, int]] = OrderedDict()


def _cache_get(guild_id: int, queue_type: str, page: int) -> tuple[bytes, int] | None:
    key = (guild_id, queue_type, page)
    val = _PAGE_CACHE.get(key)
    if val is not None:
        _PAGE_CACHE.move_to_end(key)
    return val


def _cache_set(
    guild_id: int,
    queue_type: str,
    page: int,
    png_bytes: bytes,
    total_pages: int,
) -> None:
    key = (guild_id, queue_type, page)
    _PAGE_CACHE[key] = (png_bytes, total_pages)
    _PAGE_CACHE.move_to_end(key)
    while len(_PAGE_CACHE) > _PAGE_CACHE_MAXSIZE:
        _PAGE_CACHE.popitem(last=False)


def _cache_invalidate(guild_id: int, queue_type: str) -> int:
    """Delete all cache entries for this (guild, queue_type).

    Called from `refresh_leaderboard_channel` when we just learned that
    an ELO has changed. Returns the number of entries deleted (useful
    for debugging and tests)."""
    to_remove = [k for k in _PAGE_CACHE if k[0] == guild_id and k[1] == queue_type]
    for k in to_remove:
        del _PAGE_CACHE[k]
    return len(to_remove)


def _clear_page_cache_for_tests() -> None:
    """Fully clear the cache. Used only by tests to isolate cases
    (the cache is process-wide, hence shared across tests)."""
    _PAGE_CACHE.clear()


_PAGE_LABEL_RE = re.compile(r"^\s*Page\s+(\d+)\s*/\s*(\d+)\s*$")
_ATTACH_FILENAME_RE = re.compile(r"^leaderboard_([a-z0-9_\-]+)\.png$", re.IGNORECASE)


class LeaderboardView(discord.ui.View):
    """Persistent paginated view for the leaderboard.

    Persistent = survives bot restarts. For the buttons to work after
    a restart, the view must (1) have stable `custom_id`s and (2) be
    registered in `on_ready` via `bot.add_view(LeaderboardView())`.

    The per-message state (queue_type, current page) is NOT stored
    on the bot side: it is recovered from the message itself:
      - `queue_type` -> attachment named `leaderboard_{qt}.png`
      - current page -> label of the central button `Page N / M`

    On each click, we re-read the DB to display fresh data (the
    leaderboard may have moved since the last render).
    """

    def __init__(
        self,
        *,
        page: int = 0,
        total_pages: int = 1,
        queue_type: str | None = None,
    ):
        super().__init__(timeout=None)
        self.page = page
        self.total_pages = total_pages
        self.queue_type = queue_type
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        # Order of children = order of @discord.ui.button decorators:
        # 0=prev_btn, 1=page_btn, 2=next_btn.
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.total_pages - 1
        self.page_btn.label = f"Page {self.page + 1} / {self.total_pages}"

    @staticmethod
    def _recover_state(message) -> tuple[str | None, int, int]:
        """Recover (queue_type, page_zero_indexed, total_pages) from a message.

        Robust to mocks and incomplete messages: returns (None, 0, 1) if
        nothing usable is found.
        """
        qt: str | None = None
        attachments = getattr(message, "attachments", None)
        if isinstance(attachments, (list, tuple)):
            for att in attachments:
                fn = getattr(att, "filename", "") or ""
                m = _ATTACH_FILENAME_RE.match(fn)
                if m:
                    qt = m.group(1).lower()
                    break

        page0, total = 0, 1
        components = getattr(message, "components", None)
        if isinstance(components, (list, tuple)):
            for row in components:
                children = getattr(row, "children", None)
                if not isinstance(children, (list, tuple)):
                    continue
                for comp in children:
                    label = getattr(comp, "label", None)
                    if not isinstance(label, str):
                        continue
                    m = _PAGE_LABEL_RE.match(label)
                    if m:
                        page0 = max(0, int(m.group(1)) - 1)
                        total = max(1, int(m.group(2)))
                        return qt, page0, total
        return qt, page0, total

    async def _go(self, inter, new_page: int) -> None:
        """Navigate to `new_page` (zero-based index, absolute)."""
        try:
            queue_type = self.queue_type
            total = self.total_pages

            # Persistent dispatch: the instance registered at the bot level
            # has no per-message state, so we rebuild from the message.
            recovered_from_message = False
            if queue_type is None:
                msg = getattr(inter, "message", None)
                if msg is None:
                    return
                qt, _, rec_total = self._recover_state(msg)
                if qt is None:
                    if not inter.response.is_done():
                        await inter.response.defer()
                    return
                queue_type = qt
                total = rec_total
                recovered_from_message = True

            if new_page < 0 or new_page >= total:
                if not inter.response.is_done():
                    await inter.response.defer()
                return

            if not inter.response.is_done():
                await inter.response.defer()

            # Late import: avoids the circular dependency bot <-> services
            # at module-loading time.
            from bot import db as _db

            file, new_view = await build_leaderboard_payload(
                inter.guild,
                _db,
                queue_type,
                page=new_page,
            )
            if file is None:
                return

            # Only mutate `self` if we are on the per-message instance
            # (initial queue_type non-None). On the globally registered
            # instance, mutating would pollute subsequent dispatches
            # across different guilds / queue_types.
            if not recovered_from_message:
                self.page = new_page
                if new_view is not None:
                    self.total_pages = getattr(new_view, "total_pages", total)
                self._sync_buttons()

            await inter.followup.edit_message(
                message_id=inter.message.id,
                attachments=[file],
                view=new_view,
            )
        except Exception:
            logger.exception("leaderboard_refresh exception")

    @discord.ui.button(
        emoji="◀️",
        style=discord.ButtonStyle.secondary,
        custom_id="lb:prev",
    )
    async def prev_btn(self, inter, button):
        cur = self.page
        msg = getattr(inter, "message", None)
        if msg is not None:
            _, m_page, _ = self._recover_state(msg)
            # If the message carries a usable "Page N / M" label, it is
            # authoritative over self.page (which is 0 on the registered
            # instance).
            if m_page > 0 or self.queue_type is None:
                cur = m_page
        await self._go(inter, cur - 1)

    @discord.ui.button(
        label="Page 1 / 1",
        style=discord.ButtonStyle.grey,
        disabled=True,
        custom_id="lb:page",
    )
    async def page_btn(self, inter, button):
        if not inter.response.is_done():
            await inter.response.defer()

    @discord.ui.button(
        emoji="▶️",
        style=discord.ButtonStyle.secondary,
        custom_id="lb:next",
    )
    async def next_btn(self, inter, button):
        cur = self.page
        msg = getattr(inter, "message", None)
        if msg is not None:
            _, m_page, _ = self._recover_state(msg)
            if m_page > 0 or self.queue_type is None:
                cur = m_page
        await self._go(inter, cur + 1)


# Queue display labels used in leaderboard titles.
QUEUE_DISPLAY_LABELS: dict[str, str] = {
    "open": "Open Queue",
    "advanced": "Advanced Queue",
}


async def build_leaderboard_payload(
    guild: discord.Guild,
    db,
    queue_type: str,
    *,
    with_view: bool = True,
    view_timeout: float | None = None,  # kept for back-compat, ignored (view is always persistent)
    page: int = 0,
) -> tuple[discord.File | None, discord.ui.View | None]:
    """Generate file/view for the leaderboard of the given queue_type, page `page`.

    Uses a lazy cache (see `_PAGE_CACHE`) to avoid re-rendering an already
    generated page. The cache is invalidated from
    `refresh_leaderboard_channel` whenever an ELO change occurs.
    """
    del view_timeout  # parameter kept for stable API, view always timeout=None
    repository._check_queue_type(queue_type)

    filename = f"leaderboard_{queue_type}.png"

    # Cache lookup BEFORE Mongo: if the requested page is cached, we
    # save the DB query + PIL render. The returned page is the exact
    # image rendered on the last render (consistent with the message
    # currently posted in #leaderboard).
    cached = _cache_get(guild.id, queue_type, page)
    if cached is not None:
        png_bytes, total_pages_cached = cached
        file = discord.File(
            BytesIO(png_bytes),
            filename=filename,
        )
        if not with_view:
            return file, None
        return file, LeaderboardView(
            page=page,
            total_pages=total_pages_cached,
            queue_type=queue_type,
        )

    col = repository.get_elo_col(db)
    docs = list(col.find({"queue_type": queue_type}).sort([("elo", -1), ("wins", -1), ("_id", 1)]))
    if not docs:
        return None, None

    now = datetime.now(UTC)
    all_players = []
    rank = 1
    for doc in docs:
        # Hide players inactive for more than 7 days (never-played too).
        if not is_active(doc.get("last_played"), now):
            continue
        uid = doc.get("user_id") or doc["_id"].split(":")[0]
        try:
            member = guild.get_member(int(uid))
        except (TypeError, ValueError):
            member = None
        if member is None:
            continue
        ava_url = str(member.display_avatar.replace(format="png", size=64).url)
        display_name = member.display_name or doc.get("name", uid)
        all_players.append(
            {
                "rank": rank,
                "name": display_name,
                "elo": doc["elo"],
                "wins": doc.get("wins", 0),
                "losses": doc.get("losses", 0),
                "kills": doc.get("kills", 0),
                "deaths": doc.get("deaths", 0),
                "avatar_url": ava_url,
            }
        )
        rank += 1

    if not all_players:
        return None, None

    total_pages = max(1, (len(all_players) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    loop = asyncio.get_running_loop()
    start = page * PAGE_SIZE
    chunk = all_players[start : start + PAGE_SIZE]
    # The leaderboard title includes the queue_type to distinguish the
    # multiple leaderboards that coexist in #leaderboard.
    title = f"Leaderboard {QUEUE_DISPLAY_LABELS.get(queue_type, queue_type.upper() + ' Queue')}"
    buf = await loop.run_in_executor(
        None,
        lambda: generate_leaderboard(chunk, server_name=f"{guild.name} - {title}"),
    )

    # Store the raw bytes in the cache (not the discord.File which is
    # single-use). On each cache hit, we'll wrap them in a fresh BytesIO.
    png_bytes = buf.getvalue()
    _cache_set(guild.id, queue_type, page, png_bytes, total_pages)

    file = discord.File(
        BytesIO(png_bytes),
        filename=filename,
    )

    if not with_view:
        return file, None
    return file, LeaderboardView(
        page=page,
        total_pages=total_pages,
        queue_type=queue_type,
    )


def _find_leaderboard_channel(guild: discord.Guild) -> discord.TextChannel | None:
    """Find the `#leaderboard` channel."""
    needle = LEADERBOARD_CHANNEL_NAME.lower()
    for c in guild.text_channels:
        cname = (c.name or "").lower()
        if needle in cname:
            return c
    return None


async def refresh_leaderboard_channel(
    guild: discord.Guild,
    db,
    queue_type: str,
) -> None:
    """Refresh the leaderboard of the given queue_type in `#leaderboard`.

    Per-queue debounce: an Advanced burst does not block an Open refresh."""
    repository._check_queue_type(queue_type)
    now = datetime.now(UTC)
    key = (guild.id, queue_type)
    last = _LAST_REFRESH_AT.get(key)
    if last is not None and (now - last).total_seconds() < _REFRESH_DEBOUNCE_SECONDS:
        _LAST_REFRESH_AT.move_to_end(key)
        return
    _LAST_REFRESH_AT[key] = now
    _LAST_REFRESH_AT.move_to_end(key)
    while len(_LAST_REFRESH_AT) > _MAX_GUILDS_TRACKED:
        _LAST_REFRESH_AT.popitem(last=False)

    # ELO has changed for this (guild, queue_type) AND we are about to
    # render a new page (debounce passed) -> invalidate cached pages
    # to avoid serving stale data. Page 1 freshly rendered below will
    # repopulate the cache via _cache_set.
    # Note: if the debounce had returned earlier, we do NOT invalidate
    # - the posted message stays the old one, so the cache stays consistent.
    _cache_invalidate(guild.id, queue_type)

    chan = _find_leaderboard_channel(guild)
    if chan is None:
        return

    stored_id = repository.get_leaderboard_message_id(db, guild.id, queue_type)
    if stored_id is not None:
        try:
            old_msg = await chan.fetch_message(stored_id)
            await old_msg.delete()
        except discord.NotFound:
            repository.clear_leaderboard_message_id(db, guild.id, queue_type)
        except Exception:
            logger.exception("leaderboard_refresh exception")

    # NO fallback history scan: with multiple LBs coexisting, we
    # cannot identify "which one" without the persisted state. If no
    # stored_id exists, we just post the new message.

    try:
        file, view = await build_leaderboard_payload(
            guild,
            db,
            queue_type,
            view_timeout=None,
        )
    except Exception:
        logger.exception("leaderboard_refresh exception")
        return
    if file is None:
        return

    try:
        if view is None:
            new_msg = await chan.send(file=file)
        else:
            new_msg = await chan.send(file=file, view=view)
    except Exception:
        logger.exception("leaderboard_refresh exception")
        return

    try:
        repository.set_leaderboard_message_id(db, guild.id, queue_type, new_msg.id)
    except Exception:
        logger.exception("leaderboard_refresh exception")
