"""
Integration tests for PREFIX commands (!leaderboard, !stats, !win, !lose).

Uses dpytest to simulate a full Discord environment:
  - Fake guild
  - Fake members
  - Real bot but without Discord connection

To run:
    pip install -r requirements-test.txt
    pytest test_bot_prefix.py -v
"""

import discord
import discord.ext.test as dpytest


# -- !leaderboard --
async def test_leaderboard_empty_says_no_player(discord_bot, fake_guild):
    await dpytest.message("!leaderboard")
    assert dpytest.verify().message().content("Aucun joueur enregistré.")


async def test_leaderboard_shows_one_player(discord_bot, fake_guild):
    import bot as bot_module

    col = bot_module.get_elo_col()
    member = fake_guild.members[0]
    col.insert_one(
        {
            "_id": str(member.id),
            "name": member.display_name,
            "elo": 100,
            "wins": 5,
            "losses": 2,
        }
    )

    await dpytest.message("!leaderboard")
    msg = dpytest.get_message()
    assert msg.embeds, "No embed in the response"
    embed = msg.embeds[0]
    assert "Classement" in embed.title
    assert member.display_name in embed.description
    assert "100" in embed.description  # ELO


async def test_leaderboard_orders_by_elo_desc(discord_bot, fake_guild):
    import bot as bot_module

    col = bot_module.get_elo_col()

    members = fake_guild.members[:3]
    elos = [50, 200, 100]
    for m, e in zip(members, elos, strict=True):
        col.insert_one({"_id": str(m.id), "name": m.display_name, "elo": e, "wins": 0, "losses": 0})

    await dpytest.message("!leaderboard")
    embed = dpytest.get_message().embeds[0]
    desc = embed.description

    # The 200 ELO player must appear before 100, which appears before 50
    pos_200 = desc.find(members[1].display_name)
    pos_100 = desc.find(members[2].display_name)
    pos_50 = desc.find(members[0].display_name)
    assert pos_200 < pos_100 < pos_50, "ELO desc ordering not respected"


# -- !stats --
async def test_stats_for_unknown_player(discord_bot, fake_member):
    await dpytest.message("!stats")
    assert dpytest.verify().message().contains().content("n'a pas encore joué")


async def test_stats_shows_winrate(discord_bot, fake_guild, fake_member):
    import bot as bot_module

    col = bot_module.get_elo_col()
    col.insert_one(
        {
            "_id": str(fake_member.id),
            "name": fake_member.display_name,
            "elo": 150,
            "wins": 7,
            "losses": 3,
        }
    )

    await dpytest.message("!stats")
    embed = dpytest.get_message().embeds[0]
    fields = {f.name: f.value for f in embed.fields}
    assert "150" in fields.get("🏅 ELO", "")
    assert "7" in fields.get("✅ Victoires", "")
    assert "3" in fields.get("❌ Défaites", "")
    assert "70" in fields.get("📈 Winrate", "")  # 70%


# -- !win / !lose (require permissions) --
async def test_win_refused_without_permission(discord_bot, fake_guild):
    # By default member 0 in dpytest does not have manage_guild
    target = fake_guild.members[1]
    await dpytest.message(f"!win {target.mention}")
    assert dpytest.verify().message().contains().content("Aucune permission.")


async def test_win_grants_elo_with_admin(discord_bot, fake_guild):
    """Grant manage_guild to member 0 and verify the ELO gain."""
    import bot as bot_module

    # Grant admin permissions to member 0 via a role
    admin = fake_guild.members[0]
    target = fake_guild.members[1]
    perms = discord.Permissions()
    perms.update(manage_guild=True)
    admin_role = await fake_guild.create_role(name="Admin", permissions=perms)
    await admin.add_roles(admin_role)

    await dpytest.message(f"!win {target.mention}")

    # Verify in the database - the !win prefix applies to the open queue by default
    col = bot_module.get_elo_col()
    doc = col.find_one({"_id": f"{target.id}:open"})
    assert doc is not None, "Player not created in the database"
    # Position weighting: slot 0 (player1) earns +20.
    assert doc["elo"] == 2020, f"Expected ELO 2020, got {doc['elo']}"
    assert doc["wins"] == 1


async def test_lose_floors_elo_at_zero(discord_bot, fake_guild):
    """A player at 5 ELO who loses must not drop below 0."""
    import bot as bot_module

    admin = fake_guild.members[0]
    target = fake_guild.members[1]
    partner = fake_guild.members[2]  # 2nd slot (player2)
    perms = discord.Permissions()
    perms.update(manage_guild=True)
    admin_role = await fake_guild.create_role(name="Admin", permissions=perms)
    await admin.add_roles(admin_role)

    col = bot_module.get_elo_col()
    col.insert_one(
        {
            "_id": f"{target.id}:open",
            "user_id": str(target.id),
            "queue_type": "open",
            "name": target.display_name,
            "elo": 5,
            "wins": 0,
            "losses": 0,
        }
    )
    col.insert_one(
        {
            "_id": f"{partner.id}:open",
            "user_id": str(partner.id),
            "queue_type": "open",
            "name": partner.display_name,
            "elo": 2995,
            "wins": 0,
            "losses": 0,
        }
    )

    # /lose weighted by position: slot 0 (target) -> loss=10 -> max(0, 5-10) = 0
    await dpytest.message(f"!lose {target.mention} {partner.mention}")

    doc = col.find_one({"_id": f"{target.id}:open"})
    assert doc["elo"] == 0, f"ELO must be clamped at 0, got {doc['elo']}"
    assert doc["losses"] == 1
