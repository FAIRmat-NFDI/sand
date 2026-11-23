from urllib.parse import parse_qs, urlsplit

from sand.services.deepgram import MAX_KEYTERM_TOKENS, MAX_KEYTERMS, deepgram_url


def sent(url: str) -> list[str]:
    return parse_qs(urlsplit(url).query).get('keyterm', [])


def test_the_call_goes_first_then_the_terms_in_order():
    assert sent(deepgram_url('nova-3', ['PbI2', 'MAI'])) == ['hey sand', 'PbI2', 'MAI']


def test_no_terms_and_older_models_send_none():
    assert sent(deepgram_url('nova-3', [])) == []
    assert sent(deepgram_url('nova-2', ['PbI2'])) == []


def test_what_does_not_fit_the_token_limit_is_left_out():
    terms = [f'{i:03} long chemical name here' for i in range(100)]

    kept = sent(deepgram_url('nova-3', terms))

    assert kept[:2] == ['hey sand', terms[0]]
    assert kept[1:] == terms[: len(kept) - 1]  # the first ones, in order
    # a token counted as 3 characters: never more than Deepgram allows
    assert sum(-(-len(t) // 3) for t in kept) <= MAX_KEYTERM_TOKENS


def test_at_most_a_hundred_terms():
    kept = sent(deepgram_url('nova-3', [f'm{i}' for i in range(200)]))

    assert len(kept) == MAX_KEYTERMS
