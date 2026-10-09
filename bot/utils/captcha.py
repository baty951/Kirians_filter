import random

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def build_captcha(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Build a simple arithmetic captcha.

    Returns the question text and an inline keyboard whose buttons carry
    callback data `captcha:<user_id>:<choice>:<correct>` so the handler can
    verify the answer and ensure only the joining user can solve it.
    """
    a, b = random.randint(2, 9), random.randint(2, 9)
    answer = a + b
    options = {answer}
    while len(options) < 3:
        options.add(random.randint(4, 18))
    shuffled = random.sample(list(options), len(options))

    buttons = [
        InlineKeyboardButton(
            text=str(opt), callback_data=f"captcha:{user_id}:{opt}:{answer}"
        )
        for opt in shuffled
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[buttons])
    return f"🤖 Подтвердите, что вы не бот.\nСколько будет {a} + {b}?", keyboard
