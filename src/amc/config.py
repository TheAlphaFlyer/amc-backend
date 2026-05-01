"""
AMC feature flags / static configuration.

Lightweight switches that don't need to live in the database.
Toggle these in code (and redeploy) rather than at runtime.
"""

# When False, process_treasury_expiration_penalty() becomes a no-op so the treasury is not charged the 50% penalty for expired non-Ministry jobs.
TREASURY_EXPIRATION_PENALTY_ENABLED = True

# Per-cargo multiplier applied when crediting a delivery toward a job fulfillment counter. Defaults to 1 for any cargo not listed.
CARGO_FULFILLMENT_WEIGHTS: dict[str, int] = {
    # "CARGO_KEY": multiplier"
    "Container_40ft_01": 2,
}

# Depot restock subsidy amount. Set to 0 to disable.
DEPOT_RESTOCK_SUBSIDY_AMOUNT = 10_000

# ---------------------------------------------------------------------------
# Subsidy modifiers
# ---------------------------------------------------------------------------

# Bank balance at which the wealth cut is fully applied. Scales via curve so a player who is just barely over the threshold isn't punished as hard as one who is TRULY rich
WEALTH_RICH_THRESHOLD = 5_000_000 # Below this, no wealth cut is applied and players receive full subsidies
WEALTH_RICH_CEILING = 50_000_000 # Above this, the full wealth cut is applied

# Multiplier applied to subsidies for wealthy players (see WEALTH_RICH_THRESHOLD).
# 0.0 = no subsidy, 1.0 = full subsidy. E.g. 0.25 → 75% cut.
WEALTH_RICH_SUBSIDY_MULTIPLIER = 0.25

# Multiplier applied to subsidies when the delivering player is sitting in a vehicle with detected modded parts
# 0.0 = no subsidy, 1.0 = full subsidy. E.g. 0.5 → 50% cut.
MODDED_SUBSIDY_MULTIPLIER = 0.5

# ---------------------------------------------------------------------------
# ASEAN Subsidy / Tax control based on wealth
# ---------------------------------------------------------------------------
# Below FLOOR: subsidies are zero, tax is at full strength (refill treasury).
# Above CEILING: subsidies are at full strength, tax is zero (don't over-tax).
# Subsidy interpolation is linear; tax interpolation uses TAX_CURVE_EXPONENT
TREASURY_SUBSIDY_FLOOR = 50_000_000
TREASURY_SUBSIDY_CEILING = 150_000_000

# Exponent for the tax-vs-treasury curve. 
#   < 1.0  → tax stays HIGH for most of the range and only drops sharply as the treasury approaches CEILING - 0.5 is a square root curve
#   = 1.0  → linear
#   > 1.0  → tax drops quickly off FLOOR (rarely desirable).
TAX_CURVE_EXPONENT = 0.7
