"""API-ul REST comun. Endpointurile proprii ale unui feature stau în folderul feature-ului."""

from app.routers import approvals, integrations, notifications, observability, sim, world
from app.routers import answers

COMMON_ROUTERS = [
    answers.build_router,
    world.build_router,
    approvals.build_router,
    notifications.build_router,
    sim.build_router,
    integrations.build_router,
    observability.build_router,
]
