"""Exercise installed core modules, without pytest's source-path injection."""

import subprocess
import sys


def test_installed_core_loads_strategy_schema(tmp_path):
    result = subprocess.run(
        [sys.executable, '-I', '-c',
         "from simulation_studio import strategy_schema; "
         "schema = strategy_schema('generic.BuyAndHold'); "
         "assert schema['strategy'] == 'generic.BuyAndHold'; "
         "assert isinstance(schema['parameters'], list)"],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
