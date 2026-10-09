import random

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def build_captcha(user_id: int) -> tuple[str, InlineKeyboardMarkup, int]:
    """Build a simple arithmetic captcha.

    Returns the question text, an inline keyboard and the correct answer.
    Buttons carry only `captcha:<user_id>:<choice>`: the answer is kept on the
    server, since anything in callback data is readable by the client.
    """
    a, b = random.randint(2, 9), random.randint(2, 9)
    answer = a + b
    options = {answer}
    while len(options) < 3:
        options.add(random.randint(4, 18))
    shuffled = random.sample(list(options), len(options))

    buttons = [
        InlineKeyboardButton(
            text=str(opt), callback_data=f"captcha:{user_id}:{opt}"
        )
        for opt in shuffled
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[buttons])
    return f"🤖 Подтвердите, что вы не бот.\nСколько будет {a} + {b}?", keyboard, answer
