from fastapi import HTTPException, Request


def get_bearer_token(request: Request) -> str:
    """Extract the Bearer token from the Authorization header."""
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth.removeprefix('Bearer ')
    raise HTTPException(
        status_code=401, detail='Missing or invalid Authorization header'
    )
