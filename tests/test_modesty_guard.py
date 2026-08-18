import unittest

from modesty_guard import is_flirtatious_text, is_guarded_chat


class ModestyGuardTests(unittest.TestCase):
    def test_detects_common_iraqi_flirting_words(self):
        self.assertTrue(is_flirtatious_text("هلا حبيبتي شلونج"))
        self.assertTrue(is_flirtatious_text("اشتاقلك"))
        self.assertTrue(is_flirtatious_text("أحبج"))
        self.assertTrue(is_flirtatious_text("فدوة إلك"))
        self.assertTrue(is_flirtatious_text("فديتج"))
        self.assertTrue(is_flirtatious_text("حبيبتييي"))
        self.assertTrue(is_flirtatious_text("فدووووه"))

    def test_keeps_formal_messages(self):
        self.assertFalse(is_flirtatious_text("السلام عليكم، شلون الأهل؟"))
        self.assertFalse(is_flirtatious_text("إن شاء الله أتواصل ويا الأهل بخصوص الموضوع."))

    def test_guard_never_matches_without_a_configured_chat(self):
        self.assertFalse(is_guarded_chat(123, 0))
        self.assertTrue(is_guarded_chat(123, 123))
        self.assertFalse(is_guarded_chat(123, 456))
