from ninja import NinjaAPI

api_v1 = NinjaAPI(
    title="AMC Public API",
    version="1.0.0",
    description="ASEAN Motor Club public API — game data, economy, and community statistics.",
)

# Phase 4: Economy & Real-Time Data
api_v1.add_router("/economy/", "amc.api.v1.routes.economy_router")
api_v1.add_router("/deliverypoints/", "amc.api.v1.routes.storage_router")
api_v1.add_router("/characters/", "amc.api.v1.routes.characters_router")
api_v1.add_router("/vehicles/", "amc.api.v1.routes.vehicles_router")

# Phase 5: Events & Competition
api_v1.add_router("/supply-chain-events/", "amc.api.v1.routes.supply_chain_router")

# Phase 6: Server & Community
api_v1.add_router("/server/", "amc.api.v1.routes.server_router")
api_v1.add_router("/police/", "amc.api.v1.routes.police_router")
api_v1.add_router("/rescue/", "amc.api.v1.routes.rescue_router")
api_v1.add_router("/teleports/", "amc.api.v1.routes.teleport_router")

# Phase 7: Treasury
api_v1.add_router("/treasury/", "amc_finance.api.v1.routes.treasury_router")
