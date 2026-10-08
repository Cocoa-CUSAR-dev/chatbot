"""Keeps SSO deep-link tokens out of Sentry events (US3-2 #125, F2).

The diary card's link carries a short-lived SSO token
(``{WEB_APP_URL}/sso#token=<jwt>``, src/line/router.py). Nothing here logs
that link, but Sentry's Python SDK attaches the *local variables of every
stack frame* to an exception by default. If ``push_flex`` fails -- LINE answers
429 once the monthly message quota is spent, which has happened -- the event
carries ``history_url``, ``contents`` (the Flex JSON holding the same link) and
``flex_message``. That is the worst moment for it to leak: the push failed, so
the farmer never received the link and the token in Sentry is still unspent
and valid for its full TTL.

Sentry's built-in scrubber only matches variable *names* (``token`` is on its
denylist and shows up as ``[Filtered]``; ``history_url`` is not), so it cannot
be relied on here. This scrubs by the shape of the value instead, wherever in
the event it appears.
"""

import re
from typing import Any

REDACTED = "[Filtered]"

# Both deep-link shapes: the fragment form (/sso#token=...) and the query form
# (/sso?token=...) older cards and the previous release used. The character
# class is the JWT alphabet plus the URL-safe base64 extras.
_SSO_TOKEN = re.compile(r"([?#&]token=)[A-Za-z0-9._~+/=-]+")


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return _SSO_TOKEN.sub(lambda match: match.group(1) + REDACTED, value)
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub(item) for item in value)
    return value


def scrub_sso_token(event: Any, hint: Any) -> Any:
    """``sentry_sdk.init(before_send=...)``: redact SSO tokens from the event.

    Walks the whole event instead of naming fields, so it still holds when the
    token turns up somewhere nobody thought to list (a frame variable, a
    breadcrumb, an exception message). Rebuilds containers rather than
    round-tripping through JSON, so the event keeps its original types.
    """
    return _scrub(event)
