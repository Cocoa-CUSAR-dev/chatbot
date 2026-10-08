"""US3-2 #125 (F2): the diary card's SSO link must not reach Sentry.

The shapes below are what the Sentry SDK actually shipped when push_flex
failed with a 429 -- measured against the app's own sentry_sdk.init, not
invented: the link sat in a frame variable (``history_url``), inside the Flex
JSON (``contents``) and in its repr (``flex_message``). Only a variable
*named* ``token`` was filtered by Sentry itself.
"""

import json

import sentry_sdk

from src.sentry_scrub import REDACTED, scrub_sso_token

TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl"
FRAGMENT_LINK = f"https://web-app.example/sso#token={TOKEN}"
QUERY_LINK = f"https://web-app.example/sso?token={TOKEN}"


def _event_like_the_measured_one() -> dict[str, object]:
    return {
        "exception": {
            "values": [
                {
                    "type": "ApiException",
                    "value": "(429)\nReason: Too Many Requests",
                    "stacktrace": {
                        "frames": [
                            {
                                "function": "_generate_and_push_diary",
                                "vars": {
                                    "user_id": "'user-1'",
                                    "token": "[Filtered]",
                                    "history_url": f"'{FRAGMENT_LINK}'",
                                },
                            },
                            {
                                "function": "push_flex",
                                "vars": {
                                    "contents": {
                                        "footer": {"action": {"uri": FRAGMENT_LINK}},
                                    },
                                    "flex_message": f"FlexMessage(uri='{FRAGMENT_LINK}')",
                                },
                            },
                        ]
                    },
                }
            ]
        },
        "breadcrumbs": {"values": [{"category": "http", "data": {"url": "/svc"}}]},
    }


def test_removes_the_token_from_every_place_it_appears_in_an_event() -> None:
    scrubbed = scrub_sso_token(_event_like_the_measured_one(), {})

    assert TOKEN not in json.dumps(scrubbed)


def test_keeps_the_rest_of_the_link_and_the_event_useful_for_debugging() -> None:
    scrubbed = scrub_sso_token(_event_like_the_measured_one(), {})

    exception = scrubbed["exception"]["values"][0]
    assert exception["value"] == "(429)\nReason: Too Many Requests"
    frame = exception["stacktrace"]["frames"][0]
    assert frame["vars"]["history_url"] == f"'https://web-app.example/sso#token={REDACTED}'"
    assert frame["vars"]["user_id"] == "'user-1'"


def test_scrubs_the_query_form_used_by_older_cards_too() -> None:
    assert scrub_sso_token(f"opened {QUERY_LINK}", {}) == (
        f"opened https://web-app.example/sso?token={REDACTED}"
    )


def test_scrubs_a_token_that_is_not_the_first_query_parameter() -> None:
    link = f"https://web-app.example/sso?next=/history&token={TOKEN}&x=1"

    scrubbed = scrub_sso_token(link, {})

    assert TOKEN not in scrubbed
    assert scrubbed == f"https://web-app.example/sso?next=/history&token={REDACTED}&x=1"


def test_leaves_unrelated_strings_alone() -> None:
    untouched = [
        "https://web-app.example/history?page=2",
        "retry_token=abc is a different thing",
        "token_count=5",
        "the word token on its own",
        "",
    ]

    assert [scrub_sso_token(value, {}) for value in untouched] == untouched


def test_preserves_non_string_values_and_container_types() -> None:
    event = {"count": 3, "ok": True, "missing": None, "pair": (1, "a"), "items": [1, "b"]}

    assert scrub_sso_token(event, {}) == event
    assert isinstance(scrub_sso_token(event, {})["pair"], tuple)


def test_does_not_mutate_the_event_it_was_given() -> None:
    event = _event_like_the_measured_one()
    before = json.dumps(event)

    scrub_sso_token(event, {})

    assert json.dumps(event) == before


def test_the_app_registers_it_as_the_sentry_before_send_hook() -> None:
    """The scrub is only worth anything if the app's own init uses it."""
    import src.main  # noqa: F401  -- runs the module-level sentry_sdk.init(...)

    assert sentry_sdk.get_client().options["before_send"] is scrub_sso_token
