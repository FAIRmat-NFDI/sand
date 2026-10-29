import datetime
import json

import pytest

from sand.sheet_edit.changelog import Confirmation
from sand.sheet_edit.edits import Edit, ProposedEdits, Unreadable
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
UNREADABLE = ProposedEdits(
    edits=[],
    unresolved=[Unreadable(reason='unreadable', detail='no answer', said='speed 4000')],
)
KEY = cache_key('speed 4000', b'sheet')
WHEN = datetime.datetime(2026, 10, 28, 12, 16, 5, tzinfo=datetime.UTC)


def test_cache_round_trip_keeps_proposals_and_their_jobs():
    cache = LlmCache()
    cache.add(KEY, PROPOSAL, 'job-1')
    cache.add(cache_key('other', b'sheet'), UNREADABLE, 'job-1')
    data = llm_cache_json(cache, MODEL, 'v1')
    assert set(data) == {'model', 'prompt_version', 'proposals'}
    assert set(data['proposals']) == {f'{k[0]}:{k[1]}' for k in cache.proposals}

    loaded = load_llm_cache(data, MODEL, 'v1', job='job-1')
    assert loaded.proposals == cache.proposals
    assert loaded.jobs == cache.jobs


def test_an_unreadable_answer_is_kept_only_for_its_job():
    cache = LlmCache()
    cache.add(KEY, PROPOSAL, 'job-1')
    cache.add(cache_key('other', b'sheet'), UNREADABLE, 'job-1')
    data = llm_cache_json(cache, MODEL, 'v1')
    loaded = load_llm_cache(data, MODEL, 'v1', job='job-2')
    assert loaded.proposals == {KEY: PROPOSAL}


@pytest.mark.parametrize(
    ('model', 'prompt_version'), [('other/model', 'v1'), (MODEL, 'v2')]
)
def test_another_models_or_prompts_answers_are_dropped(model, prompt_version):
    cache = LlmCache()
    cache.add(KEY, PROPOSAL, 'job-1')
    data = llm_cache_json(cache, MODEL, 'v1')
    assert load_llm_cache(data, model, prompt_version, 'job-1').proposals == {}


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
    assert load_llm_cache(data, MODEL, 'v1', 'job-1').proposals == {}


def test_a_malformed_entry_does_not_take_the_others_down():
    cache = LlmCache()
    cache.add(KEY, PROPOSAL, 'job-1')
    data = llm_cache_json(cache, MODEL, 'v1')
    data['proposals']['bad'] = {'proposal': 'x', 'job': 'job-1'}
    assert load_llm_cache(data, MODEL, 'v1', 'job-1').proposals == {KEY: PROPOSAL}


def test_confirmations_round_trip():
    confirmations = {'k1': Confirmation(by='tester', at=WHEN)}
    data = confirmations_json(confirmations)
    assert data == {'k1': {'by': 'tester', 'at': '2026-10-28T12:16:05+00:00'}}
    assert load_confirmations(data) == confirmations
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
        {'k1': {'by': 'tester', 'at': '2026-10-28T12:16:05'}},  # no timezone
        {'k1': {'by': 3, 'at': '2026-10-28T12:16:05+00:00'}},
    ],
)
def test_a_malformed_confirmation_is_refused(data):
    with pytest.raises(ValueError):
        load_confirmations(data)
