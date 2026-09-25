import urllib.parse

from fastapi import HTTPException, Request


def token_from_cookie(cookies: dict[str, str]) -> str:
    """The token in NOMAD's `Authorization` cookie ('' if absent).

    NOMAD's new GUI sets it for the whole API base path, so it reaches the
    dashboard; the value is URL-encoded 'Bearer <token>'.
    """
    cookie = urllib.parse.unquote(cookies.get('Authorization', ''))
    return cookie.removeprefix('Bearer ')


def get_bearer_token(request: Request) -> str:
    """The NOMAD token: from the Authorization header (sand's own Keycloak
    login), else from NOMAD's cookie (opened from the NOMAD GUI)."""
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth.removeprefix('Bearer ')
    token = token_from_cookie(request.cookies)
    if token:
        return token
    raise HTTPException(status_code=401, detail='Not logged in to NOMAD')
