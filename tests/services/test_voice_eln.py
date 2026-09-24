import asyncio
import json
from http import HTTPStatus

import httpx
import pytest
from fake_nomad import (
    SAND_COLLECTION_ID,
    UPLOAD_ID,
    FakeNomad,
    fake_client,
    voice_service,
)
from nomad.utils import generate_entry_id

from sand.hysprint import EXPERIMENT_INFO_LABEL, EXPERIMENT_INFO_MAINFILE
from sand.services.nomad_api import NomadAPIError, NomadAuthError, entry_ref
from sand.services.voice_eln import (
    AUDIO_INPUT_M_DEF,
    EXPERIMENT_MAINFILE,
    WRITTEN_NOTE_M_DEF,
    AudioUpload,
    normalize_audio_filename,
)

INFO = {
    'project_name': 'perov',
    'batch': 'B1',
    'subbatch': 'a',
    'first_sample': '1',
    'n_samples': 4,
}


@pytest.mark.asyncio
async def test_create_experiment_writes_collection_and_info_note():
    fake = FakeNomad()

    async with fake_client(fake) as client:
        service = voice_service()
        result = await service.create_input_collection(client, 'perov_B1_a')
        await service.add_written_note(
            client,
            UPLOAD_ID,
            json.dumps(INFO),
            collection_entry_id=SAND_COLLECTION_ID,
            label=EXPERIMENT_INFO_LABEL,
            mainfile=EXPERIMENT_INFO_MAINFILE,
        )

    assert result.upload_id == UPLOAD_ID
    assert result.entry_id == generate_entry_id(UPLOAD_ID, EXPERIMENT_MAINFILE)

    info_note = fake.archive('experiment_info.archive.json')['data']
    assert info_note['label'] == 'experiment_info'
    assert json.loads(info_note['text']) == INFO
    assert info_note['datetime']

    collection = fake.archive(EXPERIMENT_MAINFILE)['data']
    assert collection['name'] == 'perov_B1_a'
    info_entry_id = generate_entry_id(UPLOAD_ID, 'experiment_info.archive.json')
    assert collection['notes'] == [entry_ref(UPLOAD_ID, info_entry_id)]


@pytest.mark.asyncio
async def test_create_experiment_without_info_has_no_notes():
    fake = FakeNomad()

    async with fake_client(fake) as client:
        await voice_service().create_input_collection(client, 'scratch')

    collection = fake.archive(EXPERIMENT_MAINFILE)['data']
    assert 'notes' not in collection
    assert list(fake.raw_files) == [EXPERIMENT_MAINFILE]


@pytest.mark.asyncio
async def test_add_audio_stores_file_and_references_it_from_collection():
    fake = FakeNomad()

    async with fake_client(fake) as client:
        service = voice_service()
        await service.create_input_collection(client, 'perov_B1_a')
        result = await service.add_audio(
            client,
            UPLOAD_ID,
            AudioUpload(audio=b'AUDIO', filename='rec.m4a'),
            collection_entry_id=SAND_COLLECTION_ID,
        )

    audio_files = [n for n in fake.raw_files if n.endswith('_rec.m4a')]
    assert len(audio_files) == 1
    assert fake.raw_files[audio_files[0]] == b'AUDIO'
    # without a transcript, the companion is the parser's job (whisper runs)
    assert f'{audio_files[0]}.archive.json' not in fake.raw_files
    # the entry id matches the parser's deterministic companion mainfile
    assert result.entry_id == generate_entry_id(
        UPLOAD_ID, f'{audio_files[0]}.archive.json'
    )
    collection = fake.archive(EXPERIMENT_MAINFILE)['data']
    assert collection['audios'] == [entry_ref(UPLOAD_ID, result.entry_id)]


@pytest.mark.asyncio
async def test_add_audio_with_transcript_writes_pretranscribed_companion():
    # the companion is written by sand BEFORE the audio: the voice-eln
    # parser skips an existing companion, and a present transcript keeps
    # its normalizer from starting the automatic (paid) transcription
    fake = FakeNomad()

    async with fake_client(fake) as client:
        service = voice_service()
        await service.create_input_collection(client, 'perov_B1_a')
        result = await service.add_audio(
            client,
            UPLOAD_ID,
            AudioUpload(
                audio=b'AUDIO',
                filename='rec.m4a',
                transcript='UV ozone clean for all samples',
                stt_model='deepgram/nova-3',
            ),
            collection_entry_id=SAND_COLLECTION_ID,
        )

    audio_files = [n for n in fake.raw_files if n.endswith('_rec.m4a')]
    assert len(audio_files) == 1
    companion = fake.archive(f'{audio_files[0]}.archive.json')['data']
    assert companion['raw_audio'] == audio_files[0]
    assert companion['transcript'] == 'UV ozone clean for all samples'
    assert companion['transcription_status'] == 'COMPLETED'
    assert companion['transcription_meta']['stt_model'] == 'deepgram/nova-3'
    assert companion['transcription_meta']['transcribed_at']
    assert result.entry_id == generate_entry_id(
        UPLOAD_ID, f'{audio_files[0]}.archive.json'
    )
    collection = fake.archive(EXPERIMENT_MAINFILE)['data']
    assert collection['audios'] == [entry_ref(UPLOAD_ID, result.entry_id)]


@pytest.mark.asyncio
async def test_add_audio_without_collection_stores_no_file():
    # fail before storing the audio: an orphaned file could never be
    # referenced, and retries would deposit more copies
    fake = FakeNomad()

    async with fake_client(fake) as client:
        with pytest.raises(NomadAPIError) as excinfo:
            await voice_service().add_audio(
                client,
                UPLOAD_ID,
                AudioUpload(audio=b'AUDIO', filename='rec.m4a'),
                collection_entry_id=SAND_COLLECTION_ID,
            )

    assert excinfo.value.status_code == HTTPStatus.NOT_FOUND
    assert fake.raw_files == {}


@pytest.mark.asyncio
async def test_add_note_writes_step_note_and_references_it():
    fake = FakeNomad()

    async with fake_client(fake) as client:
        service = voice_service()
        await service.create_input_collection(client, 'perov_B1_a')
        result = await service.add_written_note(
            client,
            UPLOAD_ID,
            'spun coat at 2000 rpm',
            collection_entry_id=SAND_COLLECTION_ID,
        )

    note_files = [n for n in fake.raw_files if n.startswith('note_')]
    assert len(note_files) == 1
    note = fake.archive(note_files[0])['data']
    assert note['text'] == 'spun coat at 2000 rpm'
    assert note['label'] == 'step'
    assert result.entry_id == generate_entry_id(UPLOAD_ID, note_files[0])
    collection = fake.archive(EXPERIMENT_MAINFILE)['data']
    assert collection['notes'] == [entry_ref(UPLOAD_ID, result.entry_id)]


@pytest.mark.asyncio
async def test_append_writes_back_nested_mainfile():
    # PUT raw takes the directory in the URL and a bare basename in
    # file_name; a nested mainfile must be split, not passed verbatim
    fake = FakeNomad(query_results=[{'mainfile': 'exp/my_collection.archive.json'}])
    fake.raw_files['exp/my_collection.archive.json'] = json.dumps(
        {'data': {'m_def': 'x', 'name': 'manual'}}
    ).encode()

    async with fake_client(fake) as client:
        result = await voice_service().add_written_note(
            client, UPLOAD_ID, 'a step', collection_entry_id='e-nested'
        )

    collection = fake.archive('exp/my_collection.archive.json')['data']
    assert collection['notes'] == [entry_ref(UPLOAD_ID, result.entry_id)]


@pytest.mark.asyncio
async def test_append_handles_null_refs_field():
    # NOMAD deserializes explicit nulls (its "unset" value); appending to
    # a collection with '"notes": null' must not crash with a TypeError
    fake = FakeNomad()
    fake.raw_files[EXPERIMENT_MAINFILE] = json.dumps(
        {'data': {'m_def': 'x', 'name': 'n', 'notes': None}}
    ).encode()

    async with fake_client(fake) as client:
        result = await voice_service().add_written_note(
            client, UPLOAD_ID, 'a step', collection_entry_id=SAND_COLLECTION_ID
        )

    collection = fake.archive(EXPERIMENT_MAINFILE)['data']
    assert collection['notes'] == [entry_ref(UPLOAD_ID, result.entry_id)]


@pytest.mark.asyncio
async def test_append_rejects_collection_without_data_section():
    fake = FakeNomad()
    fake.raw_files[EXPERIMENT_MAINFILE] = json.dumps({'data': [1, 2]}).encode()

    async with fake_client(fake) as client:
        with pytest.raises(NomadAPIError, match='no data section'):
            await voice_service().add_written_note(
                client, UPLOAD_ID, 'a step', collection_entry_id=SAND_COLLECTION_ID
            )


@pytest.mark.asyncio
async def test_list_input_collections_returns_summaries():
    fake = FakeNomad(
        query_results=[
            {
                'upload_id': UPLOAD_ID,
                'entry_id': 'e-1',
                'entry_name': 'perov_B1_a',
            }
        ]
    )

    async with fake_client(fake) as client:
        experiments = await voice_service().list_input_collections(client)

    assert len(experiments) == 1
    assert experiments[0].upload_id == UPLOAD_ID
    assert experiments[0].entry_id == 'e-1'
    assert experiments[0].name == 'perov_B1_a'


@pytest.mark.asyncio
async def test_write_waits_for_processing_and_sends_body_once():
    # the first PUT triggers processing; the service polls the upload's
    # process_running state and PUTs each file exactly once (no re-sending
    # the body against NOMAD's processing lock)
    fake = FakeNomad(processing_polls=2)

    async with fake_client(fake) as client:
        service = voice_service()
        result = await service.create_input_collection(client, 'perov_B1_a')
        await service.add_written_note(
            client,
            UPLOAD_ID,
            json.dumps(INFO),
            collection_entry_id=SAND_COLLECTION_ID,
            label=EXPERIMENT_INFO_LABEL,
            mainfile=EXPERIMENT_INFO_MAINFILE,
        )

    assert result.upload_id == UPLOAD_ID
    assert 'experiment_info.archive.json' in fake.raw_files
    assert EXPERIMENT_MAINFILE in fake.raw_files
    files_written = 3  # collection, note, collection append
    assert fake.put_attempts == files_written


@pytest.mark.asyncio
async def test_write_retries_when_processing_starts_after_the_idle_check():
    # the check-then-PUT race: NOMAD rejects the PUT although the upload
    # looked idle; the service re-checks the status and retries
    fake = FakeNomad(blocked_writes=1)

    async with fake_client(fake) as client:
        await voice_service().create_input_collection(client, 'x')

    assert EXPERIMENT_MAINFILE in fake.raw_files


@pytest.mark.asyncio
async def test_write_to_unknown_upload_raises_not_found():
    fake = FakeNomad(upload_exists=False)

    async with fake_client(fake) as client:
        with pytest.raises(NomadAPIError) as excinfo:
            await voice_service().add_written_note(
                client, UPLOAD_ID, 'a step', collection_entry_id=SAND_COLLECTION_ID
            )

    assert excinfo.value.status_code == HTTPStatus.NOT_FOUND
    assert fake.raw_files == {}


@pytest.mark.asyncio
async def test_collection_entry_id_resolves_sand_mainfile_without_index():
    # sand's own experiments resolve deterministically, so adding to a
    # just-created (not yet indexed) experiment works with an entry id
    fake = FakeNomad()

    async with fake_client(fake) as client:
        service = voice_service()
        created = await service.create_input_collection(client, 'perov_B1_a')
        result = await service.add_written_note(
            client, UPLOAD_ID, 'a step', collection_entry_id=created.entry_id
        )

    collection = fake.archive(EXPERIMENT_MAINFILE)['data']
    assert collection['notes'] == [entry_ref(UPLOAD_ID, result.entry_id)]


@pytest.mark.asyncio
async def test_collection_entry_id_resolves_foreign_mainfile_by_query():
    # two collections in one upload: the entry id pins the chosen one
    # instead of falling back to the oldest
    fake = FakeNomad(query_results=[{'mainfile': 'second.archive.json'}])
    fake.raw_files['second.archive.json'] = json.dumps(
        {'data': {'m_def': 'x', 'name': 'second'}}
    ).encode()

    async with fake_client(fake) as client:
        result = await voice_service().add_written_note(
            client, UPLOAD_ID, 'a step', collection_entry_id='e-second'
        )

    collection = fake.archive('second.archive.json')['data']
    assert collection['notes'] == [entry_ref(UPLOAD_ID, result.entry_id)]


@pytest.mark.asyncio
async def test_unknown_collection_entry_id_raises_not_found():
    fake = FakeNomad(query_results=[])

    async with fake_client(fake) as client:
        with pytest.raises(NomadAPIError) as excinfo:
            await voice_service().add_audio(
                client,
                UPLOAD_ID,
                AudioUpload(audio=b'AUDIO', filename='rec.m4a'),
                collection_entry_id='e-gone',
            )

    assert excinfo.value.status_code == HTTPStatus.NOT_FOUND
    assert fake.raw_files == {}


@pytest.mark.asyncio
async def test_write_to_published_upload_is_rejected():
    fake = FakeNomad(published=True)

    async with fake_client(fake) as client:
        with pytest.raises(NomadAPIError, match='published'):
            await voice_service().add_written_note(
                client, UPLOAD_ID, 'a step', collection_entry_id=SAND_COLLECTION_ID
            )

    assert fake.raw_files == {}


@pytest.mark.asyncio
async def test_invalid_token_raises_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={'detail': 'unauthorized'})

    async with fake_client(handler) as client:
        with pytest.raises(NomadAuthError):
            await voice_service().create_input_collection(client, 'x')


def test_build_client_sends_bearer_token():
    client = voice_service().build_client('tok-1')
    assert client.headers['Authorization'] == 'Bearer tok-1'


def test_normalize_audio_filename():
    # Safari's MediaRecorder output is MPEG-4 audio: store it as .m4a so
    # the voice-eln parser matches it
    assert normalize_audio_filename('recording.mp4') == 'recording.m4a'
    assert normalize_audio_filename('REC.M4A') == 'REC.m4a'
    assert normalize_audio_filename('a.webm') == 'a.webm'
    assert normalize_audio_filename('notes.txt') is None
    assert normalize_audio_filename('no_extension') is None


def _fake_with_inputs(audio: dict, note: dict) -> FakeNomad:
    """An experiment whose collection references one audio and one note;
    entry 'x1' exists in the upload but is not an input of it."""
    fake = FakeNomad()
    fake.raw_files[EXPERIMENT_MAINFILE] = json.dumps(
        {
            'data': {
                'audios': [entry_ref(UPLOAD_ID, 'a1')],
                'notes': [entry_ref(UPLOAD_ID, 'n1')],
            }
        }
    ).encode()
    for entry_id, data in (
        ('a1', {'m_def': AUDIO_INPUT_M_DEF, **audio}),
        ('n1', {'m_def': WRITTEN_NOTE_M_DEF, **note}),
        ('x1', {'m_def': WRITTEN_NOTE_M_DEF, 'text': 'elsewhere'}),
    ):
        fake.entries[entry_id] = f'{entry_id}.archive.json'
        fake.raw_files[f'{entry_id}.archive.json'] = json.dumps({'data': data}).encode()
    return fake


@pytest.mark.asyncio
async def test_collect_inputs_takes_the_most_human_text_and_orders_by_time():
    fake = _fake_with_inputs(
        audio={
            'transcript': 'machine',
            'corrected_transcript': 'said',
            'intended_transcript': 'meant',
            'transcription_status': 'COMPLETED',
            'datetime': '2026-09-22T10:00:00+00:00',
        },
        note={'text': 'a note', 'datetime': '2026-09-22T09:00:00+00:00'},
    )

    async with fake_client(fake) as client:
        inputs = await voice_service().collect_inputs(
            client, UPLOAD_ID, SAND_COLLECTION_ID
        )

    assert [i.entry_id for i in inputs] == ['n1', 'a1']
    audio = inputs[1]
    assert (audio.text, audio.corrected, audio.status) == ('meant', True, 'COMPLETED')
    assert inputs[0].status is None


@pytest.mark.asyncio
async def test_collect_inputs_reports_untranscribed_audio_without_text():
    fake = _fake_with_inputs(
        audio={'transcription_status': 'PENDING'}, note={'text': 'a note'}
    )

    async with fake_client(fake) as client:
        inputs = await voice_service().collect_inputs(
            client, UPLOAD_ID, SAND_COLLECTION_ID
        )

    audio = next(i for i in inputs if i.kind == 'audio')
    assert (audio.text, audio.corrected, audio.status) == (None, False, 'PENDING')


@pytest.mark.asyncio
async def test_revise_audio_sets_and_withdraws_the_correction():
    fake = _fake_with_inputs(audio={'transcript': 'machine'}, note={'text': 'n'})

    async with fake_client(fake) as client:
        service = voice_service()
        await service.revise_input(
            client, UPLOAD_ID, 'a1', 'fixed', collection_entry_id=SAND_COLLECTION_ID
        )
        assert fake.archive('a1.archive.json')['data']['corrected_transcript'] == (
            'fixed'
        )
        await service.revise_input(
            client, UPLOAD_ID, 'a1', '  ', collection_entry_id=SAND_COLLECTION_ID
        )

    audio = fake.archive('a1.archive.json')['data']
    assert 'corrected_transcript' not in audio
    assert audio['transcript'] == 'machine'


@pytest.mark.asyncio
async def test_revise_note_rejects_empty_text():
    fake = _fake_with_inputs(audio={}, note={'text': 'keep me'})

    async with fake_client(fake) as client:
        with pytest.raises(ValueError):
            await voice_service().revise_input(
                client, UPLOAD_ID, 'n1', ' ', collection_entry_id=SAND_COLLECTION_ID
            )

    assert fake.archive('n1.archive.json')['data']['text'] == 'keep me'


@pytest.mark.asyncio
async def test_revise_rejects_an_entry_outside_the_collection():
    fake = _fake_with_inputs(audio={}, note={'text': 'n'})
    puts_before = fake.put_attempts

    async with fake_client(fake) as client:
        with pytest.raises(NomadAPIError) as excinfo:
            await voice_service().revise_input(
                client,
                UPLOAD_ID,
                'x1',
                'hijack',
                collection_entry_id=SAND_COLLECTION_ID,
            )

    assert excinfo.value.status_code == HTTPStatus.NOT_FOUND
    assert fake.put_attempts == puts_before


@pytest.mark.asyncio
async def test_revise_datetime_reorders_the_inputs():
    fake = _fake_with_inputs(
        audio={'datetime': '2026-09-22T10:00:00+00:00'},
        note={'text': 'n', 'datetime': '2026-09-22T09:00:00+00:00'},
    )

    async with fake_client(fake) as client:
        service = voice_service()
        await service.revise_input_datetime(
            client,
            UPLOAD_ID,
            'n1',
            '2026-09-22T11:00:00Z',
            collection_entry_id=SAND_COLLECTION_ID,
        )
        inputs = await service.collect_inputs(client, UPLOAD_ID, SAND_COLLECTION_ID)

    assert [i.entry_id for i in inputs] == ['a1', 'n1']


@pytest.mark.asyncio
async def test_concurrent_revisions_of_one_input_keep_both_changes():
    fake = _fake_with_inputs(audio={'transcript': 'machine'}, note={'text': 'n'})

    async def interleaving(request: httpx.Request) -> httpx.Response:
        # yield on every request so the two revisions actually overlap
        await asyncio.sleep(0)
        return fake(request)

    async with fake_client(interleaving) as client:
        service = voice_service()
        await asyncio.gather(
            service.revise_input(
                client, UPLOAD_ID, 'a1', 'fixed', collection_entry_id=SAND_COLLECTION_ID
            ),
            service.revise_input_datetime(
                client,
                UPLOAD_ID,
                'a1',
                '2026-09-22T11:00:00Z',
                collection_entry_id=SAND_COLLECTION_ID,
            ),
        )

    audio = fake.archive('a1.archive.json')['data']
    assert audio['corrected_transcript'] == 'fixed'
    assert audio['datetime'] == '2026-09-22T11:00:00+00:00'
