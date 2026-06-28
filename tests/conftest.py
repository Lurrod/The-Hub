"""
Global pytest configuration.

IMPORTANT: this file is loaded BEFORE the tests, hence before importing bot.py.
We use it to:
  1. Patch pymongo.MongoClient with mongomock (in-memory, no need for Mongo)
  2. Define dummy environment variables so that bot.py can be imported
  3. Expose dpytest fixtures for Discord integration tests
"""

import os
from unittest.mock import patch

import mongomock
import pymongo
import pytest
import pytest_asyncio

# -- 1. Dummy environment variables (before importing bot.py) --
os.environ.setdefault("DISCORD_TOKEN", "test-token-not-real")
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")


# -- 2. Patch MongoClient BEFORE bot.py is imported --
# bot.py runs `client = MongoClient(MONGO_URL)` at the top level, so we
# must replace MongoClient on the pymongo module itself.
_mongo_patcher = patch.object(pymongo, "MongoClient", mongomock.MongoClient)
_mongo_patcher.start()


# -- 2b. dpytest shim for discord.py 2.5+ --
# discord.py 2.5 passes `colors` (plural) to HTTPClient.create_role in
# addition to `color`. dpytest 0.7.0 calls make_role(**fields), which does
# not know `colors` -> TypeError. We wrap make_role to ignore unknown
# kwargs (we keep the _get_higher_locs mechanism intact by NOT modifying
# create_role itself).
def _install_dpytest_make_role_shim() -> None:
    import inspect

    from discord.ext.test import backend as _dpytest_backend

    original_make_role = _dpytest_backend.make_role
    accepted = set(inspect.signature(original_make_role).parameters)

    def make_role_compat(*args, **kwargs):
        cleaned = {k: v for k, v in kwargs.items() if k in accepted}
        return original_make_role(*args, **cleaned)

    _dpytest_backend.make_role = make_role_compat


_install_dpytest_make_role_shim()


# -- 3. Reset the in-memory database between every test --
@pytest.fixture(autouse=True)
def clean_mongo():
    """Drop all mongomock collections before every test."""
    import bot

    for name in bot.db.list_collection_names():
        bot.db.drop_collection(name)
    yield


# -- 4. dpytest fixture: simulated Discord bot --
@pytest_asyncio.fixture
async def discord_bot():
    """
    Simulated Discord bot via dpytest.

    Automatically configures:
      - 1 guild "TestGuild"
      - 1 text channel "general"
      - 3 members (TestUser0, TestUser1, TestUser2)
    """
    import discord.ext.test as dpytest

    import bot as bot_module

    # discord.py 2.x: the loop is not defined before setup_hook.
    # We force it here so dpytest can dispatch events.
    await bot_module.bot._async_setup_hook()
    # _async_setup_hook does not trigger setup_hook itself (login
    # only). In tests, we call setup_hook explicitly to load all the
    # cogs (otherwise /command and !command are not discoverable by
    # dpytest because @app_commands.command and @commands.command now
    # live in cogs).
    if not bot_module.bot.cogs:
        await bot_module.setup_hook()

    dpytest.configure(
        bot_module.bot,
        guilds=1,
        text_channels=1,
        voice_channels=0,
        members=3,
    )
    yield bot_module.bot
    await dpytest.empty_queue()


# -- 5. Reusable helpers --
@pytest.fixture
def fake_member(discord_bot):
    """Return the 1st member of the test guild."""
    import discord.ext.test as dpytest

    config = dpytest.get_config()
    return config.members[0]


@pytest.fixture
def fake_guild(discord_bot):
    import discord.ext.test as dpytest

    config = dpytest.get_config()
    return config.guilds[0]


@pytest.fixture
def mongo_db():
    """In-memory mongomock database, isolated per test."""
    return mongomock.MongoClient(tz_aware=True).db
