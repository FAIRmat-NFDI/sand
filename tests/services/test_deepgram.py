from urllib.parse import parse_qs, urlsplit

from sand.services.deepgram import deepgram_url


def _params(url):
    return parse_qs(urlsplit(url).query)


def test_keyterms_with_nova_3():
    params = _params(deepgram_url('nova-3', ['NiOx', 'Me-4PACz']))
    assert params['model'] == ['nova-3']
    assert params['interim_results'] == ['true']
    assert params['keyterm'] == ['NiOx', 'Me-4PACz', 'hey sand']


def test_no_keyterms_for_older_models():
    params = _params(deepgram_url('nova-2', ['NiOx']))
    assert 'keyterm' not in params


def test_spaces_are_encoded():
    assert 'keyterm=hey+sand' in deepgram_url('nova-3', [])
