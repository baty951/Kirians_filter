import random

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

OPTIONS = 6  # buttons per question, laid out in two rows
# emoji -> how the question names it (accusative)
EMOJI_NAMES = {
    "🐱": "кота", "🐶": "собаку", "🍎": "яблоко", "🚗": "машину", "⚽": "мяч",
    "🌳": "дерево", "🎈": "шар", "🔑": "ключ", "🍕": "пиццу", "🎸": "гитару",
}


def _arithmetic() -> tuple[str, dict[int, str], int]:
    kind = random.choice("+-×")
    if kind == "+":
        a, b = random.randint(2, 9), random.randint(2, 9)
        answer = a + b
    elif kind == "-":
        a = random.randint(6, 18)
        b = random.randint(2, a - 2)
        answer = a - b
    else:
        a, b = random.randint(2, 6), random.randint(2, 6)
        answer = a * b
    # Distractors stay near the answer so every button looks plausible.
    near = [v for v in range(max(1, answer - 8), answer + 9) if v != answer]
    values = [answer, *random.sample(near, OPTIONS - 1)]
    return f"Сколько будет {a} {kind} {b}?", {v: str(v) for v in values}, answer


def _emoji() -> tuple[str, dict[int, str], int]:
    shown = random.sample(list(EMOJI_NAMES), OPTIONS)
    target = random.choice(shown)
    # Callback values are random ids, so the answer cannot be read off the button position or text.
    values = random.sample(range(10, 100), OPTIONS)
    labels = dict(zip(values, shown))
    answer = next(value for value, emoji in labels.items() if emoji == target)
    return f"Нажмите на {EMOJI_NAMES[target]}.", labels, answer


def build_captcha(user_id: int) -> tuple[str, InlineKeyboardMarkup, int]:
    """Build a captcha question: arithmetic (+, -, ×) or "press the <object>".

    Returns the question text, an inline keyboard and the correct answer.
    Buttons carry only `captcha:<user_id>:<choice>`: the answer is kept on the
    server, since anything in callback data is readable by the client.
    """
    question, labels, answer = random.choice((_arithmetic, _arithmetic, _emoji))()
    order = random.sample(list(labels), len(labels))
    buttons = [
        InlineKeyboardButton(text=labels[value], callback_data=f"captcha:{user_id}:{value}") for value in order
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[buttons[:3], buttons[3:]])
    return f"🤖 Подтвердите, что вы не бот.\n{question}", keyboard, answer
