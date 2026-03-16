def get_cargo_bonus(cargo_key: str, payment: int, damage: float) -> int:
    """Calculate the game-level bonus for a cargo delivery.

    The game deposits bonuses on top of Net_Payment into the player's wallet,
    but Net_Payment only reports the base amount. This function computes the
    bonus so our tracked payment matches the actual wallet deposit.

    Returns the bonus amount the game deposits ON TOP of Net_Payment.
    """
    match cargo_key:
        case "Log_Oak_12ft":
            # Safety Bonus: 100% of base at 0 damage, scales linearly to 0 at full damage
            return int(payment * (1.0 - damage))
        case _:
            return 0
