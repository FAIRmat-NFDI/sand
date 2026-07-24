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
        data = await service.extract(text)
    finally:
        await service.close()

    print(json.dumps(data, indent=1))
    print(f'--> extraction returned {len(data)} top-level field(s)',
          file=sys.stderr)

    from sand.services.hysprint_export import build_archive

    archive = build_archive(data)
    print(f'--> archive data has {len(archive["data"])} field(s) after '
          'pruning (incl. m_def)', file=sys.stderr)


if __name__ == '__main__':
    asyncio.run(main())
