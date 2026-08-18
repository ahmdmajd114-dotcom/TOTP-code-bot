import unittest

from modesty_guard import is_flirtatious_text


class ModestyGuardTests(unittest.TestCase):
    def test_detects_common_iraqi_flirting_words(self):
        self.assertTrue(is_flirtatious_text("هلا حبيبتي شلونج"))
        self.assertTrue(is_flirtatious_text("اشتاقلك"))
        self.assertTrue(is_flirtatious_text("أحبج"))

    def test_keeps_formal_messages(self):
        self.assertFalse(is_flirtatious_text("السلام عليكم، شلون الأهل؟"))
        self.assertFalse(is_flirtatious_text("إن شاء الله أتواصل ويا الأهل بخصوص الموضوع."))
