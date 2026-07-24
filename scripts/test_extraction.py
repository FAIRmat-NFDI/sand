#!/usr/bin/env python3
"""Call the extraction service directly, without the NOMAD app.

Usage:
    BLABLADOR_API_KEY=<key> python scripts/test_extraction.py <textfile>

Prints the raw extraction, then (if perla-extract is installed) how many
entries survive the NOMAD conversion filters.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from sand.services.extraction import ExtractionService  # noqa: E402


async def main() -> None:
    key = os.environ.get('BLABLADOR_API_KEY')
    if not key:
        sys.exit('Set the BLABLADOR_API_KEY environment variable')
    if len(sys.argv) < 2:
        sys.exit(f'Usage: {sys.argv[0]} <textfile>')
    text = Path(sys.argv[1]).read_text()

    service = ExtractionService(api_key=key)
    try:
        cells = await service.extract(text)
    finally:
        await service.close()

    print(json.dumps(cells, indent=1))
    print(f'--> extraction returned {len(cells)} cell(s)', file=sys.stderr)

    if not cells:
        return
    try:
        from sand.services.perovskite_export import (
            convert_cells_to_nomad_entries,
        )
    except ImportError:
        print('--> perla-extract not installed, skipping conversion',
              file=sys.stderr)
        return
    entries = convert_cells_to_nomad_entries(cells, source_text=text)
    print(f'--> {len(entries)} entry(ies) survive the NOMAD conversion',
          file=sys.stderr)


if __name__ == '__main__':
    asyncio.run(main())
