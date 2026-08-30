from collections.abc import AsyncIterator

from fastapi import HTTPException, Request

from sand.services.voice_eln import VoiceElnService, VoiceElnSession


def get_bearer_token(request: Request) -> str:
    """Extract the Bearer token from the Authorization header."""
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth.removeprefix('Bearer ')
    raise HTTPException(
        status_code=401, detail='Missing or invalid Authorization header'
    )


async def voice_session(request: Request) -> AsyncIterator[VoiceElnSession]:
    """Request-scoped voice-eln session, authenticated as the caller."""
    service: VoiceElnService = request.app.state.voice_eln
    async with service.session(get_bearer_token(request)) as session:
        yield session
