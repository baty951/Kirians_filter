import re
import unittest

from bot.utils.captcha import OPTIONS, build_captcha
from support import solve_captcha

USER_ID = 42


class CaptchaGeneratorTests(unittest.TestCase):
    def test_every_question_has_six_distinct_buttons_and_exactly_one_correct_answer(self):
        kinds = set()
        for _ in range(600):
            question, keyboard, answer = build_captcha(USER_ID)
            buttons = [button for row in keyboard.inline_keyboard for button in row]
            self.assertEqual([len(row) for row in keyboard.inline_keyboard], [3, 3])
            values = [int(button.callback_data.rsplit(":", 1)[1]) for button in buttons]
            self.assertEqual(len(values), OPTIONS)
            self.assertEqual(len(set(values)), OPTIONS)
            self.assertEqual(len({button.text for button in buttons}), OPTIONS)
            for button in buttons:
                self.assertRegex(button.callback_data, rf"^captcha:{USER_ID}:\d+$")
            self.assertEqual(solve_captcha(question, keyboard), answer)  # independent solver agrees
            found = re.search(r"\d+ ([+\-×]) \d+", question)
            kinds.add(found[1] if found else "emoji")
        self.assertEqual(kinds, {"+", "-", "×", "emoji"})

    def test_subtraction_is_never_negative_or_trivial(self):
        for _ in range(300):
            question, _, answer = build_captcha(USER_ID)
            if " - " in question:
                self.assertGreaterEqual(answer, 2)
