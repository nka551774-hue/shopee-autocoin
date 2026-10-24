import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("args", [["--help"], ["status"]])
def test_readonly_commands_work_without_third_party_packages(args):
    result = subprocess.run([sys.executable, "-S", "-m", "autocoin", *args],
                            capture_output=True, timeout=15,
                            env={**os.environ, "PYTHONUTF8": "1"})
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


@pytest.mark.parametrize("args", [
    ["--hours", "nan"], ["--hours", "inf"], ["--hours", "-1"],
    ["--hours", "0"], ["--adb-port", "0"], ["--adb-port", "65536"],
])
def test_invalid_options_fail_before_loading_device_driver(args):
    result = subprocess.run([sys.executable, "-S", "-m", "autocoin", *args],
                            capture_output=True, timeout=15,
                            env={**os.environ, "PYTHONUTF8": "1"})
    assert result.returncode == 2
    assert b"ModuleNotFoundError" not in result.stderr
