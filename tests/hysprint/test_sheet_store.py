import hashlib
import json

import pytest
from fake_nomad import (
    SAND_COLLECTION_ID,
    UPLOAD_ID,
    FakeNomad,
    fake_client,
    voice_service,
)
from nomad.utils import generate_entry_id

from sand.hysprint.sheet import DERIVED_SHEET_MAINFILE, EXTRACTED_JSON_MAINFILE
from sand.hysprint.sheet_store import SheetStore
from sand.services.nomad_api import entry_ref
from sand.services.voice_eln import EXPERIMENT_MAINFILE

SHEET_ID = generate_entry_id(UPLOAD_ID, DERIVED_SHEET_MAINFILE)
ARCHIVE = {'data': {'name': 'perov_B1_a'}}


def _fake_with_parse() -> FakeNomad:
    """A fake whose sheet parse produced one sample entry."""
    fake = FakeNomad()
    fake.processed[SHEET_ID] = ['sample-1']
    fake.entries['sample-1'] = 'sample_1.archive.json'
    fake.raw_files['sample_1.archive.json'] = b'{}'
    return fake


async def _store_extracted(store: SheetStore, client, xlsx: bytes):
    return await store.store_extracted(
        client,
        UPLOAD_ID,
        xlsx,
        SAND_COLLECTION_ID,
        archive=ARCHIVE,
        input_entry_ids=['note-1'],
    )


@pytest.mark.asyncio
async def test_store_extracted_writes_sheet_record_and_derived_entries():
    fake = _fake_with_parse()

    async with fake_client(fake) as client:
        voice = voice_service()
        await voice.create_input_collection(client, 'perov_B1_a')
        handle, replaced_edits = await _store_extracted(
            SheetStore(voice), client, b'v1'
        )

    assert handle.entry_id == SHEET_ID
    assert replaced_edits is False
    assert fake.raw_files[DERIVED_SHEET_MAINFILE] == b'v1'
    record = fake.archive(EXTRACTED_JSON_MAINFILE)
    assert record['archive'] == ARCHIVE
    assert record['xlsx_sha256'] == hashlib.sha256(b'v1').hexdigest()
    assert record['input_entry_ids'] == ['note-1']
    assert fake.archive(EXPERIMENT_MAINFILE)['data']['derived_entries'] == [
        entry_ref(UPLOAD_ID, SHEET_ID),
        entry_ref(UPLOAD_ID, 'sample-1'),
    ]


@pytest.mark.asyncio
async def test_store_extracted_over_hand_edits_reports_and_cleans_stale_entries():
    fake = _fake_with_parse()
    # a parsed entry pointing at sand's own file must never be deleted
    fake.processed[SHEET_ID].append('own-file')
    fake.entries['own-file'] = EXPERIMENT_MAINFILE

    async with fake_client(fake) as client:
        voice = voice_service()
        store = SheetStore(voice)
        await voice.create_input_collection(client, 'perov_B1_a')
        await _store_extracted(store, client, b'v1')
        fake.raw_files[DERIVED_SHEET_MAINFILE] = b'edited by hand'
        _, replaced_edits = await _store_extracted(store, client, b'v2')

    assert replaced_edits is True
    assert 'sample_1.archive.json' not in fake.raw_files
    assert fake.raw_files[DERIVED_SHEET_MAINFILE] == b'v2'
    record = fake.archive(EXTRACTED_JSON_MAINFILE)
    assert record['xlsx_sha256'] == hashlib.sha256(b'v2').hexdigest()


@pytest.mark.asyncio
async def test_store_uploaded_same_bytes_writes_nothing():
    fake = _fake_with_parse()

    async with fake_client(fake) as client:
        voice = voice_service()
        store = SheetStore(voice)
        await voice.create_input_collection(client, 'perov_B1_a')
        await _store_extracted(store, client, b'v1')
        puts_before = fake.put_attempts
        handle, changed = await store.store_uploaded(
            client, UPLOAD_ID, b'v1', SAND_COLLECTION_ID
        )

    assert handle.entry_id == SHEET_ID
    assert changed is False
    assert fake.put_attempts == puts_before


@pytest.mark.asyncio
async def test_store_uploaded_keeps_the_extraction_record():
    fake = _fake_with_parse()

    async with fake_client(fake) as client:
        voice = voice_service()
        store = SheetStore(voice)
        await voice.create_input_collection(client, 'perov_B1_a')
        await _store_extracted(store, client, b'v1')
        record = fake.raw_files[EXTRACTED_JSON_MAINFILE]
        _, changed = await store.store_uploaded(
            client, UPLOAD_ID, b'edited', SAND_COLLECTION_ID
        )

    assert changed is True
    assert fake.raw_files[DERIVED_SHEET_MAINFILE] == b'edited'
    # the pristine record stays, so its hash now marks the sheet as edited
    assert fake.raw_files[EXTRACTED_JSON_MAINFILE] == record
    assert json.loads(record)['xlsx_sha256'] != hashlib.sha256(b'edited').hexdigest()


@pytest.mark.asyncio
async def test_read_returns_none_before_extraction():
    fake = FakeNomad()

    async with fake_client(fake) as client:
        voice = voice_service()
        await voice.create_input_collection(client, 'perov_B1_a')
        store = SheetStore(voice)
        before = await store.read(client, UPLOAD_ID, SAND_COLLECTION_ID)
        await _store_extracted(store, client, b'v1')
        after = await store.read(client, UPLOAD_ID, SAND_COLLECTION_ID)

    assert before is None
    assert after == b'v1'
