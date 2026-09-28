import datetime as dt

import pytest
import requests

from sealed.tcgcsv import TcgcsvClient, TcgcsvError, parse_last_updated


class FakeResponse:
    def __init__(self, status_code, json_body=None, text=""):
        self.status_code = status_code
        self._json = json_body
        self.text = text

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._json


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.urls = []

    def get(self, url, timeout):
        self.urls.append(url)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client(responses):
    return TcgcsvClient(session=FakeSession(responses), sleep=lambda s: None)


def test_parse_last_updated_go_layout():
    # mtgban parses this file with Go layout "2006-01-02T15:04:05-0700".
    assert parse_last_updated("2026-09-27T20:04:11+0000\n") == dt.datetime(
        2026, 9, 27, 20, 4, 11, tzinfo=dt.timezone.utc
    )


def test_user_agent_is_set():
    c = client([])
    assert c.session.headers["User-Agent"].startswith("sealed-tracker/")


def test_404_is_empty_collection():
    assert client([FakeResponse(404)]).prices(123) == []


def test_retries_then_succeeds():
    c = client([requests.ConnectionError("blip"), FakeResponse(200, {"success": True, "results": [{"a": 1}]})])
    assert c.groups() == [{"a": 1}]


def test_gives_up_after_retries():
    with pytest.raises(TcgcsvError):
        client([FakeResponse(500)] * 3).groups()


def test_success_false_raises():
    with pytest.raises(TcgcsvError):
        client([FakeResponse(200, {"success": False, "errors": ["x"], "results": []})]).groups()
