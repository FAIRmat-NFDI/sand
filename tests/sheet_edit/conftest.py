import pytest
from synthetic_sheet import letter, with_formula_results


@pytest.fixture
def with_results(synthetic_xlsx) -> bytes:
    """Row 3 with Excel-stored formula results, so the writer may
    recompute them."""
    return with_formula_results(
        synthetic_xlsx,
        {
            f'{letter("Experiment Info", "Nomad ID")}3': 'KIT_Synth_20250101_B1_0_1',
            f'{letter("5: Close Space Sublimation", "Deposition Time [s]")}3': '600',
        },
    )
