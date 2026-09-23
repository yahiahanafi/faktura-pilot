from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from faktura_pilot.cli import _doctor_dependency, main


class DoctorCliTests(unittest.TestCase):
    def test_doctor_reports_missing_tesseract_as_warning_when_uia_is_ready(self):
        checks = {
            "UI Automation": "pywinauto available",
            "Window": "'Fakturama - D:\\Yahia', PID 5228, HWND 1245772",
            "Version": "2.2.0",
            "Language": "English (visible File and Data labels found)",
            "DPI awareness": "process is DPI aware",
        }
        output = io.StringIO()
        with (
            patch(
                "faktura_pilot.automation.windows.WindowsFakturamaGateway.diagnose",
                return_value=checks,
            ) as diagnose,
            patch(
                "faktura_pilot.cli._doctor_dependency",
                side_effect=["0.6.9", "0.3.13", "5.0.0"],
            ),
            patch("faktura_pilot.cli.shutil.which", return_value=None),
            redirect_stdout(output),
        ):
            status = main(["doctor"])

        self.assertEqual(status, 0)
        self.assertIn("Tesseract executable is missing", output.getvalue())
        self.assertIn(
            "UI Automation is ready; actions requiring OCR will stop safely",
            output.getvalue(),
        )
        self.assertNotIn("—", output.getvalue())
        diagnose.assert_called_once_with(None)

    def test_doctor_passes_an_explicit_executable_path(self):
        path = Path("C:/Program Files/Fakturama2/Fakturama.exe")
        checks = {
            "UI Automation": "pywinauto available",
            "Window": "'Fakturama', PID 1, HWND 2",
            "Version": "2.2.0",
            "Language": "English (visible File and Data labels found)",
            "DPI awareness": "process is DPI aware",
        }
        output = io.StringIO()
        with (
            patch(
                "faktura_pilot.automation.windows.WindowsFakturamaGateway.diagnose",
                return_value=checks,
            ) as diagnose,
            patch("faktura_pilot.cli._doctor_dependency", return_value="installed"),
            patch("faktura_pilot.cli.shutil.which", return_value="C:/Tools/tesseract.exe"),
            redirect_stdout(output),
        ):
            status = main(["doctor", "--fakturama-exe", str(path)])

        self.assertEqual(status, 0)
        diagnose.assert_called_once_with(path)

    def test_dependency_initialization_error_is_reported_instead_of_crashing(self):
        with patch(
            "faktura_pilot.cli.importlib.import_module",
            side_effect=RuntimeError("broken native initialization"),
        ):
            result = _doctor_dependency("cv2")

        self.assertIn("RuntimeError", result)
        self.assertIn("broken native initialization", result)


if __name__ == "__main__":
    unittest.main()
