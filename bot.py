import logging
import os
import sys
from datetime import UTC
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
from pymongo import MongoClient
from pymongo.collection import Collection

from services import elo_calc, repository
from services.leaderboard_refresh import (
    LeaderboardView,
)
from services.riot_api import HenrikDevClient

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    """Configure root logging. Idempotent — clears prior handlers.

    Split stdout / stderr: DEBUG+INFO -> stdout, WARNING+ -> stderr.
    PM2 captures stdout -> out.log and stderr -> error.log, so as long
    as nothing is abnormal only out.log fills up.

    Without this, the root logger stays at WARNING by default with
    Python's minimal format (no timestamp/level/module). In prod on PM2,
    `logger.info(...)` logs would be silently lost.
    """
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] [%(name)s] %(message)s")
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.DEBUG)
    stdout_handler.addFilter(lambda r: r.levelno < logging.WARNING)
    stdout_handler.setFormatter(fmt)
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level, logging.INFO))
    root.handlers.clear()
    root.addHandler(stdout_handler)
    root.addHandler(stderr_handler)


# ── Load .env if present (without crashing if python-dotenv missing) ──
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# ── Configuration ──────────────────────────────────────────────
TOKEN = os.environ.get("DISCORD_TOKEN")
MONGO_URL = os.environ.get("MONGO_URL")

# Fail-fast at startup if MONGO_URL is missing or empty. Without this
# guard, MongoClient(None, ...) silently falls back to pymongo's
# default `mongodb://localhost:27017/` -- in prod on Kimsufi this can
# point the bot to a missing Mongo instance, with a
# `serverSelectionTimeoutMS` error 5s later and no clear message about
# the cause (missing env var after a `pm2 restart` without --update-env).
if not MONGO_URL:
    raise RuntimeError("MONGO_URL environment variable not set")

ELO_START = elo_calc.ELO_START
MAPS = list(elo_calc.MAPS)

# ELO weighting by player position (slot 1..5) for /win and /lose.
# The first slot takes the biggest gain / smallest loss.
WIN_DELTAS_BY_SLOT: tuple[int, ...] = (20, 18, 17, 16, 15)
LOSE_DELTAS_BY_SLOT: tuple[int, ...] = (10, 10, 12, 13, 15)

# ── MongoDB ────────────────────────────────────────────────────
# retryWrites/retryReads are True by default since pymongo 4.x but we
# make them explicit for network-blip resilience. serverSelectionTimeoutMS=5000
# avoids blocking >30s when Mongo is down -> Discord returns "The application
# did not respond". connectTimeoutMS=5000 caps the initial handshake.
client: "MongoClient[dict[str, Any]]" = MongoClient(
    MONGO_URL,
    tz_aware=True,
    tzinfo=UTC,
    retryWrites=True,
    retryReads=True,
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
)
db = client["elobot"]


def get_elo_col() -> Collection:
    return repository.get_elo_col(db)


def get_bypass_col() -> Collection:
    return repository.get_bypass_col(db)


def get_player(col, member: discord.Member, queue_type: str):
    return repository.get_or_create_player(
        col,
        member.id,
        queue_type,
        member.display_name,
        initial_elo=ELO_START,
    )


# Slash choices shared by all ELO/leaderboard commands.
_QUEUE_CHOICES = [
    app_commands.Choice(name="Open", value="open"),
    app_commands.Choice(name="Advanced", value="advanced"),
]


def get_bypass_role(guild_id):
    return repository.get_bypass_role(db, guild_id)


def set_bypass_role(guild_id, role_id):
    repository.set_bypass_role(db, guild_id, role_id)


def has_access(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.manage_guild:
        return True
    role_id = get_bypass_role(interaction.guild_id)
    return bool(role_id and any(r.id == role_id for r in interaction.user.roles))


# ── Bot ────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


# ── V2 cogs loading ───────────────────────────────────────────────
riot_client = HenrikDevClient()


async def _load_v2_cogs() -> None:
    from cogs.admin import setup as setup_admin
    from cogs.applications import setup as setup_applications
    from cogs.elo_admin import setup as setup_elo_admin
    from cogs.match import setup as setup_match
    from cogs.moderation import setup as setup_moderation
    from cogs.prefix_legacy import setup as setup_prefix_legacy
    from cogs.queue_v2 import setup as setup_queue_v2
    from cogs.riot_link import setup as setup_riot_link
    from cogs.rules import setup as setup_rules
    from cogs.stats import setup as setup_stats

    await setup_riot_link(bot, db, riot_client)
    match_cog = await setup_match(bot, db, henrik_client=riot_client)
    await setup_queue_v2(bot, db, on_full=match_cog.on_queue_full)
    await setup_applications(bot, db)
    await setup_admin(bot, db)
    await setup_elo_admin(bot, db)
    await setup_stats(bot, db)
    await setup_moderation(bot, db)
    await setup_prefix_legacy(bot, db)
    await setup_rules(bot, db)


@bot.event
async def setup_hook():
    # Load essential cogs (queue_v2, match, riot_link). Without them,
    # the bot starts in degraded mode (missing slash commands, queue
    # inaccessible) without clearly reporting the error. We log + raise
    # to fail fast rather than running a useless bot.
    try:
        await _load_v2_cogs()
    except Exception:
        # Re-raise: Discord.py will stop startup. Better than a bot
        # silently broken in prod.
        logger.critical("[setup_hook] CRITICAL: cog loading failed", exc_info=True)
        raise


_synced_once = False


@bot.event
async def on_ready():
    global _synced_once

    if _synced_once:
        # on_ready can fire multiple times (WS reconnects): we sync only
        # on the first ready to avoid spamming Discord and uselessly
        # waiting for the global propagation time (~1h). Same for
        # `add_view` which would reference new View instances on each
        # reconnect (minor memory leak on frequent reconnects).
        logger.info("Bot reconnected: %s (slash sync skipped)", bot.user)
        return

    # First on_ready only: registration of the leaderboard view.
    # Other persistent views (Welcome, ApplicationReview, CloseTicket,
    # Report, Queue) are registered by their respective cogs during
    # setup_hook (cf. cogs/applications.py and cogs/queue_v2.py).
    # LeaderboardView: pagination for the persistent leaderboard
    # messages posted in #leaderboard. Without this registration, the
    # prev/next buttons stop working after a bot restart.
    bot.add_view(LeaderboardView())

    # Fast sync on a specific guild if DEV_GUILD_ID is defined.
    # Otherwise, global sync (can take up to 1h to propagate).
    dev_guild_id = os.getenv("DEV_GUILD_ID")
    if dev_guild_id:
        guild = discord.Object(id=int(dev_guild_id))
        tree.copy_global_to(guild=guild)
        synced = await tree.sync(guild=guild)
        logger.info("Bot connected: %s (ID: %s)", bot.user, bot.user.id)
        logger.info("%d slash commands synced on guild %s.", len(synced), dev_guild_id)
    else:
        synced = await tree.sync()
        logger.info("Bot connected: %s (ID: %s)", bot.user, bot.user.id)
        logger.info("%d slash commands synced (global, propagation up to 1h).", len(synced))
    _synced_once = True


if __name__ == "__main__":
    _configure_logging()
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN environment variable not set")
    bot.run(TOKEN)
