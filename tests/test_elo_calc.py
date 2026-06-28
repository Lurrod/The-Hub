"""Tests unitaires de services.elo_calc - logique pure."""

from services import elo_calc


# ── Constantes ────────────────────────────────────────────────────
def test_maps_list_not_empty():
    assert len(elo_calc.MAPS) >= 5
    assert "Ascent" in elo_calc.MAPS


def test_elo_start_is_2000():
    """Default starting ELO is 2000 (was 0). Players are seeded at 2000
    when they first appear in any queue."""
    from services.elo_calc import ELO_START

    assert ELO_START == 2000


# ── Pondération par Rating 2.0 (pro queue) ────────────────────────
def test_weight_constants():
    """Bornes asymétriques sur le delta final, toutes du bon signe."""
    assert elo_calc.ELO_WEIGHT_C == 15
    assert elo_calc.ELO_RATING_BASELINE == 1.0
    assert elo_calc.ELO_MIN_ROUNDS_FOR_WEIGHT == 6
    assert elo_calc.ELO_WIN_MIN == 17
    assert elo_calc.ELO_WIN_MAX == 26
    assert elo_calc.ELO_LOSS_MIN == -15  # plus petite perte
    assert elo_calc.ELO_LOSS_MAX == -22  # plus grosse perte
    # Bornes du bon signe => signe du résultat préservé.
    assert elo_calc.ELO_WIN_MIN > 0 and elo_calc.ELO_LOSS_MIN < 0


def test_weighted_delta_average_player_is_flat():
    """Un joueur exactement à la baseline (1.0) prend le ±20 plat
    (20 est dans [17, 26] et -20 dans [-22, -15])."""
    assert elo_calc.compute_weighted_delta(1.0, win=True) == 20
    assert elo_calc.compute_weighted_delta(1.0, win=False) == -20


def test_weighted_delta_carry_is_clamped():
    """Un carry plafonne au max gain (+26) / min perte (-15)."""
    # win: 20 + 15*0.4 = 26 ; loss: -20 + 6 = -14 -> clampé à -15
    assert elo_calc.compute_weighted_delta(1.40, win=True) == 26
    assert elo_calc.compute_weighted_delta(1.40, win=False) == -15
    # Au-delà, toujours bornes.
    assert elo_calc.compute_weighted_delta(2.00, win=True) == 26
    assert elo_calc.compute_weighted_delta(2.00, win=False) == -15


def test_weighted_delta_underperformer_is_clamped():
    """Un joueur en difficulté plafonne au min gain (+17) / max perte (-22)."""
    # win: 20 - 7.5 = 12.5 -> 17 ; loss: -20 - 7.5 = -27.5 -> -22
    assert elo_calc.compute_weighted_delta(0.50, win=True) == 17
    assert elo_calc.compute_weighted_delta(0.50, win=False) == -22


def test_weighted_delta_mid_value_not_clamped():
    """Valeur intermédiaire : pente linéaire, dans les bornes."""
    # win: 20 + 15*0.2 = 23 ; loss: -20 + 3 = -17
    assert elo_calc.compute_weighted_delta(1.20, win=True) == 23
    assert elo_calc.compute_weighted_delta(1.20, win=False) == -17


def test_weighted_delta_within_bounds():
    """Tout gain est dans [17, 26], toute perte dans [-22, -15]."""
    for r in (0.0, 0.3, 0.5, 0.85, 1.0, 1.2, 1.5, 2.5):
        win = elo_calc.compute_weighted_delta(r, win=True)
        loss = elo_calc.compute_weighted_delta(r, win=False)
        assert 17 <= win <= 26
        assert -22 <= loss <= -15


def test_weighted_delta_custom_baseline():
    """La baseline est ajustable (anti-inflation si la moyenne dérive)."""
    # baseline 1.1 : un joueur à 1.1 est désormais 'moyen' -> ±20 plat.
    assert elo_calc.compute_weighted_delta(1.1, win=True, baseline=1.1) == 20
    assert elo_calc.compute_weighted_delta(1.1, win=False, baseline=1.1) == -20
