import sys
from pathlib import Path

import pytest

# the synthetic sheet lives with the sheet_edit tests
sys.path.insert(0, str(Path(__file__).parent / 'sheet_edit'))

from synthetic_sheet import build  # noqa: E402

REAL_KIT_SHEET = Path(__file__).parents[1] / 'docs' / 'kit_real_sheet.xlsx'


@pytest.fixture(scope='session')
def synthetic_xlsx() -> bytes:
    return build()


@pytest.fixture(scope='session')
def real_kit_xlsx() -> bytes:
    """The private KIT sheet; only on machines that have docs/."""
    if not REAL_KIT_SHEET.exists():
        pytest.skip('docs/kit_real_sheet.xlsx not present (local-only)')
    return REAL_KIT_SHEET.read_bytes()
