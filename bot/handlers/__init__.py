from aiogram import Router

from bot.handlers import admin, common, moderation, settings, welcome


def setup_routers() -> Router:
    """Aggregate all feature routers. Order matters: command handlers first,
    then the catch-all moderation handler last."""
    router = Router()
    router.include_router(common.router)
    router.include_router(admin.router)
    router.include_router(settings.router)
    router.include_router(welcome.router)
    router.include_router(moderation.router)  # catch-all, must be last
    return router
