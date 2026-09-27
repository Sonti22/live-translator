"""AI meeting notes (meeting_notes.summarize / to_markdown) against a local mock of the Responses API."""
import json

import pytest

import meeting_notes
from mocks import free_port
from voice_clone import CloneError

KEY = "sk-test"
ROUTE = ("POST", "/v1/responses")
NOTES = {"title": "Созвон по релизу", "summary": "Обсудили сроки.", "decisions": ["Релиз в пятницу"],
         "action_items": ["Сурен: обновить README"]}


def response(*parts):
    return {"id": "resp_1", "output": [{"type": "reasoning", "content": None},
                                       {"type": "message", "content": list(parts)}]}


def test_summarize(http_server):
    http_server.routes[ROUTE] = (200, response({"type": "output_text", "text": json.dumps(NOTES)}))
    transcript = "[00:00] Я: Привет\n        → Hi"
    assert meeting_notes.summarize(KEY, transcript, None) == NOTES

    [req] = http_server.requests
    assert req.headers["Authorization"] == f"Bearer {KEY}"
    assert req.headers["Content-Type"] == "application/json"
    assert json.loads(req.body) == {
        "model": meeting_notes.MODEL, "instructions": meeting_notes.INSTRUCTIONS, "input": transcript,
        "text": {"format": {"type": "json_schema", "name": "meeting_notes", "schema": meeting_notes.SCHEMA,
                            "strict": True}},
    }


def test_long_transcript_keeps_the_end(http_server, monkeypatch):
    monkeypatch.setattr(meeting_notes, "MAX_CHARS", 10)
    http_server.routes[ROUTE] = (200, response({"type": "output_text", "text": json.dumps(NOTES)}))
    meeting_notes.summarize(KEY, "0123456789ABCDEFGHIJ", None)
    assert json.loads(http_server.requests[-1].body)["input"] == "…\nABCDEFGHIJ"


@pytest.mark.parametrize("status, payload, message", [
    (401, {"error": {"code": "invalid_api_key"}}, "OpenAI отклонил ключ"),
    (403, {"error": {"code": "unsupported_country_region_territory"}}, "OpenAI отклонил ключ"),
    (429, b"rate limited", "HTTP 429 rate limited"),
    (200, {"output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]}, "пустой ответ"),
])
def test_errors(http_server, status, payload, message):
    http_server.routes[ROUTE] = (status, payload)
    with pytest.raises(CloneError, match=message):
        meeting_notes.summarize(KEY, "text", None)


def test_no_connection(monkeypatch):
    monkeypatch.setattr(meeting_notes, "API_URL", f"http://127.0.0.1:{free_port()}/v1/responses")
    with pytest.raises(CloneError, match="Нет связи с OpenAI"):
        meeting_notes.summarize(KEY, "text", None)


def test_to_markdown():
    md = meeting_notes.to_markdown(NOTES, "line 1\nline 2\n", "Live Translator — 27.09.2026 10:00")
    assert md.splitlines() == [
        "# Созвон по релизу", "", "Live Translator — 27.09.2026 10:00", "",
        "## Кратко", "", "Обсудили сроки.", "",
        "## Решения", "", "- Релиз в пятницу", "",
        "## Задачи", "", "- Сурен: обновить README", "",
        "## Стенограмма", "", "```", "line 1", "line 2", "```",
    ]


def test_to_markdown_without_notes():
    assert meeting_notes.to_markdown({}, "x", "Header").splitlines() == [
        "# Header", "", "Header", "", "## Стенограмма", "", "```", "x", "```"]
