import sys
from pathlib import Path

import pytest

# the synthetic sheet lives with the sheet_edit tests
sys.path.insert(0, str(Path(__file__).parent.parent / 'sheet_edit'))

from synthetic_sheet import build  # noqa: E402


@pytest.fixture(scope='session')
def synthetic_xlsx() -> bytes:
    return build()
