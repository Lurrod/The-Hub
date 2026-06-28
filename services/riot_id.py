"""Riot ID helpers. Pure logic (no Discord nor Mongo)."""

from __future__ import annotations


def parse_riot_id(riot_id: str) -> tuple[str, str]:
    """
    Parse "Name#TAG" -> ("Name", "TAG"). Tolerates spaces in the name.

    Raises:
        ValueError if the format is invalid.
    """
    if not isinstance(riot_id, str) or "#" not in riot_id:
        raise ValueError("Invalid format. Expected: Name#TAG")
    name, _, tag = riot_id.rpartition("#")
    name = name.strip()
    tag = tag.strip()
    if not name or not tag:
        raise ValueError("Invalid format. Expected: Name#TAG")
    if len(tag) > 5 or len(name) > 16:
        raise ValueError("Name too long (max 16) or tag too long (max 5)")
    return name, tag
