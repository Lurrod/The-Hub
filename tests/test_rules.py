"""Tests for the rules cog + the rules-acceptance repository."""

from unittest.mock import AsyncMock, MagicMock

import discord

from cogs.rules import RulesCog, RulesView, build_rules_embed
from services import repository


# ── Repository ────────────────────────────────────────────────────
def test_has_accepted_rules_false_by_default():
    import bot as bot_module

    assert repository.has_accepted_rules(bot_module.db, 1) is False


def test_record_then_has_accepted_rules_true():
    import bot as bot_module

    repository.record_rules_acceptance(bot_module.db, 1, display_name="Bob")
    assert repository.has_accepted_rules(bot_module.db, 1) is True


def test_record_rules_acceptance_idempotent():
    import bot as bot_module

    repository.record_rules_acceptance(bot_module.db, 1, display_name="Bob")
    repository.record_rules_acceptance(bot_module.db, 1, display_name="Bob2")
    col = repository.get_rules_col(bot_module.db)
    assert col.count_documents({"_id": "1"}) == 1


def _fake_rules_interaction(user_id: int = 1, display_name: str = "User"):
    inter = MagicMock()
    inter.user = MagicMock()
    inter.user.id = user_id
    inter.user.display_name = display_name
    inter.channel = MagicMock()
    inter.channel.mention = "#general"
    inter.channel.send = AsyncMock(return_value=MagicMock(id=999))
    inter.response = MagicMock()
    inter.response.send_message = AsyncMock()
    return inter


def test_build_rules_embed_contains_all_rules():
    embed = build_rules_embed()
    text = (embed.description or "") + " ".join(f.value for f in embed.fields)
    assert "typing en jeu" in text
    assert "insultes" in text
    assert "tbag" in text
    assert "troll" in text
    assert "tickets-reports" in text


async def test_accept_button_custom_id_is_fixed():
    import bot as bot_module

    view = RulesView(bot_module.db)
    assert view.accept_btn.custom_id == "rules:accept"


async def test_accept_button_records_acceptance():
    import bot as bot_module

    db = bot_module.db
    view = RulesView(db)
    inter = _fake_rules_interaction(user_id=7, display_name="Seven")

    await view._accept_callback(inter)

    assert repository.has_accepted_rules(db, 7) is True
    inter.response.send_message.assert_awaited_once()
    assert inter.response.send_message.call_args.kwargs.get("ephemeral") is True


async def test_rules_command_posts_in_current_channel():
    import bot as bot_module

    cog = RulesCog(MagicMock(), bot_module.db)
    inter = _fake_rules_interaction()

    await cog.rules.callback(cog, inter)

    inter.channel.send.assert_awaited_once()
    kwargs = inter.channel.send.call_args.kwargs
    assert isinstance(kwargs["embed"], discord.Embed)
    assert kwargs["view"] is cog.rules_view
    inter.response.send_message.assert_awaited_once()
    assert inter.response.send_message.call_args.kwargs.get("ephemeral") is True


async def test_rules_command_handles_none_channel():
    import bot as bot_module

    cog = RulesCog(MagicMock(), bot_module.db)
    inter = _fake_rules_interaction()
    inter.channel = None

    await cog.rules.callback(cog, inter)

    # No crash; an ephemeral error is sent instead of posting.
    inter.response.send_message.assert_awaited_once()
    assert inter.response.send_message.call_args.kwargs.get("ephemeral") is True
