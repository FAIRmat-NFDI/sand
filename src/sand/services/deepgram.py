from urllib.parse import urlencode

DEEPGRAM_LIVE_URL = 'wss://api.deepgram.com/v1/listen'

# Voice mode's call: helps the stop command be heard in the transcript.
VOICE_KEYTERMS = ['hey sand']


def deepgram_url(model: str, keyterms: list[str]) -> str:
    params = [('model', model), ('interim_results', 'true'), ('smart_format', 'true')]
    # keyterm prompting exists for Nova-3 only; older models reject it
    if model.startswith('nova-3'):
        params += [('keyterm', term) for term in [*keyterms, *VOICE_KEYTERMS]]
    return f'{DEEPGRAM_LIVE_URL}?{urlencode(params)}'
