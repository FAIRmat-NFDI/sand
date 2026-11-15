import datetime
import json

import pytest

from sand.sheet_edit.apply import Answer
from sand.sheet_edit.changelog import Confirmation
from sand.sheet_edit.edits import Edit, ProposedEdits, Unreadable
from sand.sheet_edit.reads import Reads, to_json
from sand.sheet_edit.session import (
    LlmCache,
    cache_key,
    confirmations_json,
    llm_cache_json,
    load_confirmations,
    load_llm_cache,
    read_confirmations,
)

MODEL = 'gemini/test'
EDIT = Edit(
    target='C025', rows='all', anchor=None, old='3000', new='4000', said='speed 4000'
)
PROPOSAL = ProposedEdits(edits=[EDIT], unresolved=[])
ANSWER = Answer(PROPOSAL)
UNREADABLE = Answer(
    ProposedEdits(
        edits=[],
        unresolved=[
            Unreadable(reason='unreadable', detail='no answer', said='speed 4000')
        ],
    )
)
KEY = cache_key('speed 4000', b'sheet')
READS = Reads(
    None, 'columns', (('DMF', (('C013', 3), ('C013', 4))),), (), (('C014', 5),)
)
WHEN = datetime.datetime(2026, 10, 28, 12, 16, 5, tzinfo=datetime.UTC)


def test_cache_round_trip_keeps_proposals_and_their_jobs():
    cache = LlmCache()
    cache.add(KEY, ANSWER, 'job-1')
    cache.add(cache_key('other', b'sheet'), UNREADABLE, 'job-1')
    data = llm_cache_json(cache, MODEL, 'v1')
    assert set(data) == {'model', 'prompt_version', 'proposals'}
    assert set(data['proposals']) == {f'{k[0]}:{k[1]}' for k in cache.answers}

    loaded = load_llm_cache(data, MODEL, 'v1', job='job-1')
    assert loaded.answers == cache.answers
    assert loaded.jobs == cache.jobs


def test_what_was_saved_on_its_sheet_travels_with_its_answer():
    saved = Answer(PROPOSAL, (('holds 2000, not 3000',),), READS, 7)
    cache = LlmCache()
    cache.add(KEY, saved, 'job-1')
    cache.add(cache_key('other', b'sheet'), ANSWER, 'job-1')
    data = llm_cache_json(cache, MODEL, 'v1')
    # an answer saved without them stays as small as before
    other = data['proposals'][':'.join(cache_key('other', b'sheet'))]
    assert set(other) == {'proposal', 'job'}

    loaded = load_llm_cache(json.loads(json.dumps(data)), MODEL, 'v1', job='job-1')
    assert loaded.answers[KEY] == saved
    # merged and published with the job's entries
    merged = LlmCache()
    merged.update(loaded.of_job('job-1'))
    assert merged.answers == loaded.answers


@pytest.mark.parametrize('stored', [None, 'x', [], [['a'], ['b']], [[1]]])
def test_an_answer_without_a_usable_old_check_still_loads(stored):
    cache = LlmCache()
    cache.add(KEY, ANSWER, 'job-1')
    data = llm_cache_json(cache, MODEL, 'v1')
    if stored is not None:
        data['proposals'][f'{KEY[0]}:{KEY[1]}']['old_read'] = stored

    loaded = load_llm_cache(data, MODEL, 'v1', job='job-1')
    assert loaded.answers == {KEY: ANSWER}


@pytest.mark.parametrize(
    'reads',
    [
        None,
        'x',
        {},
        {**to_json(READS), 'held': {'DMF': [['C013', 'three']]}},
        {**to_json(READS), 'empty': [[3, 'C014']]},
        {**to_json(READS), 'structure': 5},
    ],
)
def test_a_malformed_read_set_makes_an_answer_for_its_own_sheet_only(reads):
    cache = LlmCache()
    cache.add(KEY, Answer(PROPOSAL, seq='x'), 'job-1')
    data = llm_cache_json(cache, MODEL, 'v1')
    data['proposals'][f'{KEY[0]}:{KEY[1]}']['reads'] = reads

    loaded = load_llm_cache(data, MODEL, 'v1', job='job-1')
    assert loaded.answers == {KEY: ANSWER}


def test_the_shared_cache_keeps_the_newest_answers_and_the_jobs_chain():
    cache = LlmCache()
    keys = [cache_key('speed 4000', f'sheet {n}'.encode()) for n in range(12)]
    for seq, key in enumerate(keys, 1):
        cache.add(key, Answer(PROPOSAL, seq=seq), 'job-1')
    cache.add(cache_key('other', b'sheet'), ANSWER, 'job-1')

    kept = cache.trimmed({keys[0]}, per_words=3)
    assert set(kept.answers) == {
        keys[0],
        *keys[-3:],
        cache_key('other', b'sheet'),
    }


def test_an_unreadable_answer_is_kept_only_for_its_job():
    cache = LlmCache()
    cache.add(KEY, ANSWER, 'job-1')
    cache.add(cache_key('other', b'sheet'), UNREADABLE, 'job-1')
    data = llm_cache_json(cache, MODEL, 'v1')
    loaded = load_llm_cache(data, MODEL, 'v1', job='job-2')
    assert loaded.answers == {KEY: ANSWER}


@pytest.mark.parametrize(
    ('model', 'prompt_version'), [('other/model', 'v1'), (MODEL, 'v2')]
)
def test_another_models_or_prompts_answers_are_dropped(model, prompt_version):
    cache = LlmCache()
    cache.add(KEY, ANSWER, 'job-1')
    data = llm_cache_json(cache, MODEL, 'v1')
    assert load_llm_cache(data, model, prompt_version, 'job-1').answers == {}


@pytest.mark.parametrize(
    'data',
    [
        None,
        'text',
        {'model': MODEL, 'prompt_version': 'v1'},
        {'model': MODEL, 'prompt_version': 'v1', 'proposals': []},
        {'model': MODEL, 'prompt_version': 'v1', 'proposals': {'a:b': 'x'}},
        {'model': MODEL, 'prompt_version': 'v1', 'proposals': {'a:b': {'job': 'j'}}},
        {
            'model': MODEL,
            'prompt_version': 'v1',
            'proposals': {'a:b': {'proposal': {'edits': 1}, 'job': 'j'}},
        },
        {
            'model': MODEL,
            'prompt_version': 'v1',
            'proposals': {'a:b': {'proposal': PROPOSAL.model_dump(), 'job': 3}},
        },
    ],
)
def test_a_malformed_cache_is_an_empty_cache(data):
    assert load_llm_cache(data, MODEL, 'v1', 'job-1').answers == {}


def test_a_malformed_entry_does_not_take_the_others_down():
    cache = LlmCache()
    cache.add(KEY, ANSWER, 'job-1')
    data = llm_cache_json(cache, MODEL, 'v1')
    data['proposals']['bad'] = {'proposal': 'x', 'job': 'job-1'}
    assert load_llm_cache(data, MODEL, 'v1', 'job-1').answers == {KEY: ANSWER}


def test_confirmations_round_trip():
    confirmations = {'k2': Confirmation(by='tester', at=WHEN, input='n2')}
    data = confirmations_json(confirmations)
    assert data == {
        'k2': {'by': 'tester', 'at': '2026-10-28T12:16:05+00:00', 'input': 'n2'}
    }
    assert load_confirmations(data) == confirmations
    # a record made before confirmations named their input is left out
    old = {'k1': {'by': 'tester', 'at': '2026-10-28T12:16:05+00:00'}}
    assert load_confirmations({**data, **old}) == confirmations
    assert read_confirmations(json.dumps(data).encode()) == data
    assert read_confirmations(None) == {}


@pytest.mark.parametrize(
    'raw', [b'', b'{"k1": {"by": "tester", "at": "2026', b'[]', b'3']
)
def test_a_damaged_confirmations_file_is_refused_not_empty(raw):
    with pytest.raises(ValueError, match='confirmations file'):
        read_confirmations(raw)


@pytest.mark.parametrize(
    'data',
    [
        {'k1': 'tester'},
        {'k1': {'by': 'tester'}},
        {'k1': {'by': '', 'at': '2026-10-28T12:16:05+00:00'}},
        {'k1': {'by': 'tester', 'at': 'yesterday'}},
        {'k1': {'by': 'tester', 'at': '2026-10-28T12:16:05', 'input': 'n1'}},  # no tz
        {'k1': {'by': 'tester', 'at': '2026-10-28T12:16:05+00:00', 'input': 3}},
        {'k1': {'by': 'tester', 'at': '2026-10-28T12:16:05+00:00', 'input': None}},
        {'k1': {'by': 3, 'at': '2026-10-28T12:16:05+00:00'}},
    ],
)
def test_a_malformed_confirmation_is_refused(data):
    with pytest.raises(ValueError):
        load_confirmations(data)


def test_answers_saved_before_seq_keep_the_newest_by_their_place():
    cache = LlmCache()
    keys = [cache_key('speed 4000', f'sheet {n}'.encode()) for n in range(5)]
    for key in keys:
        cache.add(key, ANSWER, 'job-1')
    assert set(cache.trimmed(set(), per_words=2).answers) == set(keys[-2:])
