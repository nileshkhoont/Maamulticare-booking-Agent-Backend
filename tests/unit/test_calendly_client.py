"""Covers _extract_error_codes against the exact real Calendly error bodies observed in
production (2026-10-09), not guessed shapes — see calendly_sync_service.py for how the extracted
codes are used to tell a permanent, slot-specific conflict ("already_filled") apart from
everything else.
"""

import httpx

from app.integrations.calendly.client import _extract_error_codes


def _response(body: str, status_code: int = 400) -> httpx.Response:
    return httpx.Response(status_code=status_code, content=body, request=httpx.Request("POST", "https://api.calendly.com/invitees"))


def test_extract_error_codes_already_filled():
    body = (
        '{"title":"Invalid Argument","message":"The supplied parameters are invalid.",'
        '"details":[{"parameter":"event.start_time","message":"That start time has been filled",'
        '"code":"already_filled"}]}'
    )
    assert _extract_error_codes(_response(body)) == ["already_filled"]


def test_extract_error_codes_invalid_location_choice():
    body = (
        '{"title":"Invalid Argument","message":"Specified location kind is not configured for '
        'this event type.","details":[{"parameter":"event.location_configuration.location",'
        '"message":"invalid location choice","code":"invalid_location_choice"}]}'
    )
    assert _extract_error_codes(_response(body)) == ["invalid_location_choice"]


def test_extract_error_codes_no_details_key():
    # Real 401 body — no "details" at all.
    body = '{"title":"Unauthenticated","message":"The access token is invalid"}'
    assert _extract_error_codes(_response(body, status_code=401)) == []


def test_extract_error_codes_not_json():
    assert _extract_error_codes(_response("<html>502 Bad Gateway</html>", status_code=502)) == []


def test_extract_error_codes_multiple_details():
    body = (
        '{"title":"Invalid Argument","message":"multi","details":['
        '{"parameter":"a","message":"m1","code":"code_one"},'
        '{"parameter":"b","message":"m2","code":"code_two"}]}'
    )
    assert _extract_error_codes(_response(body)) == ["code_one", "code_two"]
