from aiogram import Router

from bot.handlers import admin, badwords, common, settings, welcome


def setup_routers() -> Router:
    """Content checks run in outer middleware before these command routers."""
    router = Router()
    router.include_router(common.router)
    router.include_router(admin.router)
    router.include_router(badwords.router)
    router.include_router(settings.router)
    router.include_router(welcome.router)
    return router
