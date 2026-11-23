import math
from urllib.parse import urlencode

DEEPGRAM_LIVE_URL = 'wss://api.deepgram.com/v1/listen'

# Voice mode's call: helps the stop command be heard in the transcript.
VOICE_KEYTERMS = ['hey sand']

# Deepgram refuses the whole connection above 500 tokens across all
# keyterms (and above 100 terms). Its tokenizer is not public: a token is
# counted as 3 characters, which overestimates, plus margin.
MAX_KEYTERMS = 100
MAX_KEYTERM_TOKENS = 450
CHARS_PER_TOKEN = 3


def deepgram_url(model: str, keyterms: list[str]) -> str:
    """keyterms most important first: what does not fit is left out."""
    params = [('model', model), ('interim_results', 'true'), ('smart_format', 'true')]
    # keyterm prompting exists for Nova-3 only; older models reject it.
    # No keyterms configured: none at all, also not voice mode's.
    if keyterms and model.startswith('nova-3'):
        params += [('keyterm', term) for term in _fitting([*VOICE_KEYTERMS, *keyterms])]
    return f'{DEEPGRAM_LIVE_URL}?{urlencode(params)}'


def _fitting(terms: list[str]) -> list[str]:
    kept, tokens = [], 0
    for term in terms:
        cost = math.ceil(len(term) / CHARS_PER_TOKEN)
        if len(kept) == MAX_KEYTERMS or tokens + cost > MAX_KEYTERM_TOKENS:
            break
        kept.append(term)
        tokens += cost
    return kept
