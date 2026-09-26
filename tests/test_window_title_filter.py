import unittest

from faktura_pilot.automation.windows import _is_fakturama_window_title


class FakturamaWindowTitleTests(unittest.TestCase):
    def test_notification_dialogs_are_not_detected_as_application_windows(self) -> None:
        for title in (
            "Fakturama: automation complete",
            "Fakturama: manual review required",
            "  FAKTURAMA: MANUAL REVIEW REQUIRED  ",
        ):
            with self.subTest(title=title):
                self.assertFalse(_is_fakturama_window_title(title))
        self.assertTrue(_is_fakturama_window_title("Fakturama 2.2.0"))
        self.assertFalse(_is_fakturama_window_title("Report about Fakturama"))


if __name__ == "__main__":
    unittest.main()
