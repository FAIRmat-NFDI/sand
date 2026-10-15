from urllib.parse import urlencode

# No `encoding` parameter: the server then decodes the browser's
# WebM/Opus chunks itself (ffmpeg). Sending encoding=linear16 would make
# it expect raw PCM instead.
LIVE_PARAMS = [('interim_results', 'true'), ('language', 'en')]


def live_transcript_url(base_url: str) -> str:
    return f'{base_url}?{urlencode(LIVE_PARAMS)}'
