import json

import httpx
import pytest

from sand.services.nomad_upload import ARCHIVE_FILENAME, NomadUploader
from tests.services.test_voice_eln import BASE_URL, UPLOAD_ID, _FakeNomad


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE_URL)


@pytest.mark.asyncio
async def test_upload_writes_archive_and_returns_urls():
    fake = _FakeNomad()

    async with _client(fake) as client:
        result = await NomadUploader(BASE_URL, retry_interval_s=0, write_timeout_s=5).upload_with_client(
            client, {'data': {'name': 'cell-1'}}
        )

    assert result.upload_id == UPLOAD_ID
    assert UPLOAD_ID in result.entry_url
    archive = json.loads(fake.raw_files[ARCHIVE_FILENAME])
    assert archive == {'data': {'name': 'cell-1'}}


@pytest.mark.asyncio
async def test_upload_waits_out_processing_and_sends_body_once():
    # the pipeline path shares the processing-state-aware raw write
    fake = _FakeNomad(processing_polls=2)

    async with _client(fake) as client:
        await NomadUploader(BASE_URL, retry_interval_s=0, write_timeout_s=5).upload_with_client(client, {'data': {}})

    assert ARCHIVE_FILENAME in fake.raw_files
    assert fake.put_attempts == 1
