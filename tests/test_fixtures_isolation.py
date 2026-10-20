from __future__ import annotations

import gc
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CHECK = (
    "import sys\n"
    "import tests.conftest\n"
    "import tests.helpers.factories\n"
    "loaded = sorted(n for n in sys.modules if n == 'app.main' or n.startswith('app.api'))\n"
    "print(','.join(loaded))\n"
)


def test_the_shared_fixtures_do_not_import_the_rest_layer():
    result = subprocess.run(
        [sys.executable, "-c", CHECK],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_the_objects_created_while_collecting_tests_are_kept_out_of_the_cycle_collector():
    assert gc.get_freeze_count() > 0
