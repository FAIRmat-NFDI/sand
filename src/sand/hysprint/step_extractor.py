from sand.hysprint.steps import (
    FILL_SYSTEM,
    SELECT_SYSTEM,
    fill_schema,
    normalize_variants,
    select_schema,
)
from sand.services.extraction_service import ExtractionService


async def select_step(runner: ExtractionService, text: str) -> str:
    data = await runner.extract(
        text=text,
        extraction_schema=select_schema(),
        system_prompt=SELECT_SYSTEM,
    )
    return data['step_type']


async def fill_step(runner: ExtractionService, text: str, step_type: str) -> dict:
    slot = await runner.extract(
        text=text,
        extraction_schema=fill_schema(step_type),
        system_prompt=FILL_SYSTEM,
        instruction_text=f'STEP TYPE: {step_type}',
    )
    return normalize_variants(slot)


async def extract_step(runner: ExtractionService, text: str) -> dict:
    """One step's narration -> {step_type, variants} (the slot append_step takes)."""
    step_type = await select_step(runner, text)
    return await fill_step(runner, text, step_type)
