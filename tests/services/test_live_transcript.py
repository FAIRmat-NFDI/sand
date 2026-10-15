from urllib.parse import parse_qs, urlsplit

from sand.services.live_transcript import live_transcript_url


def test_live_transcript_url_asks_for_english_interims():
    url = live_transcript_url('ws://whisper-host:8090/v1/listen')
    parts = urlsplit(url)
    assert (
        f'{parts.scheme}://{parts.netloc}{parts.path}'
        == 'ws://whisper-host:8090/v1/listen'
    )
    assert parse_qs(parts.query) == {'interim_results': ['true'], 'language': ['en']}


def test_live_transcript_url_leaves_encoding_to_the_server():
    # With encoding=linear16 the server expects raw PCM; the browser sends WebM.
    assert 'encoding' not in live_transcript_url('ws://h/v1/listen')
