import asyncio

import httpx
import pytest

from services import lastfm_service


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_):
        return None
    monkeypatch.setattr(lastfm_service.asyncio, "sleep", instant)


def run_top_track(responses):
    """Serve the given responses in order; return (result, number of requests)."""
    calls = []

    def handler(request):
        calls.append(request)
        return responses[min(len(calls), len(responses)) - 1]

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await lastfm_service.get_top_track("Artist", client)
    return asyncio.run(go()), len(calls)


def ok(track):
    return httpx.Response(200, json={"toptracks": {"track": track}})


def test_rate_limit_error_in_body_is_retried():
    result, n = run_top_track([
        httpx.Response(200, json={"error": 29, "message": "Rate Limit Exceeded"}),
        ok([{"name": "Hit"}]),
    ])
    assert (result, n) == ("Hit", 2)


def test_single_track_object():
    assert run_top_track([ok({"name": "Only Song"})]) == ("Only Song", 1)


def test_not_found_is_not_retried():
    result, n = run_top_track([httpx.Response(400, json={"error": 6, "message": "The artist you supplied could not be found"})])
    assert (result, n) == (None, 1)


def test_non_json_is_not_retried():
    assert run_top_track([httpx.Response(200, text="<html>oops</html>")]) == (None, 1)


def test_server_errors_retried_then_give_up():
    assert run_top_track([httpx.Response(503)]) == (None, 3)


def test_empty_toptracks():
    assert run_top_track([ok([])]) == (None, 1)
