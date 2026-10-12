from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

router = Router(name="common")

HELP_TEXT = (
    "<b>Kirians Filter</b> — бот для модерации чатов.\n\n"
    "<b>Команды администратора</b> (ответом на сообщение или с ID):\n"
    "• /warn [причина] — выдать предупреждение\n"
    "• /unwarn — сбросить предупреждения\n"
    "• /warns — показать число предупреждений\n"
    "• /mute [время] [причина] — заглушить (1m–365d; без времени — навсегда)\n"
    "• /unmute — снять заглушение\n"
    "• /ban [причина] — забанить\n"
    "• /unban — разбанить\n"
    "• /kick — удалить из чата\n"
    "• /purge — удалить сообщения от отвечаемого до текущего\n"
    "• /approve, /unapprove — доверенный участник: автомодерация его не трогает\n"
    "• /approved — список доверенных\n\n"
    "<b>Для всех</b>:\n"
    "• /report [причина] — пожаловаться на сообщение (ответом), админы получат уведомление\n\n"
    "<b>Фильтры и настройки</b>:\n"
    "• /settings — меню настроек чата\n"
    "• /addword слово — добавить запрещённое слово\n"
    "• /delword слово — удалить запрещённое слово\n"
    "• /langs — статус языкового фильтра и список письменностей\n"
    "• /blocklang arabic chinese — блокировать письменности\n"
    "• /allowlang arabic — разблокировать письменности\n"
    "• /allowdomain youtube.com — разрешить домен в фильтре ссылок\n"
    "• /denydomain youtube.com — убрать домен из разрешённых\n"
    "• /domains — разрешённые домены\n"
    "• /pending — не прошедшие капчу (в муте)\n"
    "• /setwelcome текст — текст приветствия ({name} — имя; без текста — сброс)\n"
    "• /setlog — назначить текущий чат лог-каналом\n"
)


@router.channel_post(CommandStart())
@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 Привет! Я модерирую чаты: фильтрую спам и запрещённые слова, "
        "ограничиваю флуд, проверяю новых участников капчей.\n\n"
        "Добавьте меня в чат и выдайте права администратора, затем /help."
    )


@router.channel_post(Command("help"))
@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)
