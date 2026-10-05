# lead-bot/tests/test_writer_and_guard.py
# The writer: the real OpenAI SDK against a fake HTTP layer, so the request it builds and the
# response it parses are checked against the API's shapes. The guard: every check blocks on its
# own, and the plain-code backstops block insurance and price talk even when Jev scores it low.
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from openai import OpenAI
from text_fakes import SETTINGS
from typesafe_sdk import NoulAnswer

from lead_bot.guard import BLOCK_AT_PROBABILITY, MAX_REPLY_CHARACTERS, REPLY_CHECKS, find_reply_problems
from lead_bot.jev import Answer
from lead_bot.text_script import conversation_facts
from lead_bot.writer import OpenAIWriter, Turn, WriterError

# ---- the writer ----


def openai_response_body(status: str = "completed", text: str = "  About an hour.  ", **extra: Any) -> Any:
    """A Responses API result: one assistant message holding one output_text block."""
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 1_790_000_000,
        "model": "gpt-6-luna",
        "status": status,
        "output": [
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        **extra,
    }


def writer_with_fake_http(handler: Any) -> OpenAIWriter:
    # The OpenAI SDK is built on plain httpx, unlike the TypeSafe SDK (httpx2).
    client = OpenAI(
        api_key="test-key", http_client=httpx.Client(transport=httpx.MockTransport(handler)), max_retries=0
    )
    return OpenAIWriter("gpt-6-luna", client=client)


def test_the_writer_request_and_its_parsed_answer() -> None:
    request_details: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        request_details["url"] = str(request.url)
        request_details["body"] = json.loads(request.content)
        return httpx.Response(200, json=openai_response_body())

    history = [
        Turn("assistant", "Hi Dana, this is the automated assistant..."),
        Turn("user", "How long does it take?"),
    ]
    reply = writer_with_fake_http(handler).write(
        facts="Inspections take about 60 minutes.", history=history, reason="off_script_question"
    )

    assert reply == "About an hour."
    body = request_details["body"]
    assert request_details["url"].endswith("/v1/responses")
    assert body["model"] == "gpt-6-luna"
    assert "Inspections take about 60 minutes." in body["instructions"]
    assert "Never discuss insurance" in body["instructions"]
    assert body["reasoning"] == {"effort": "none"}
    assert body["max_output_tokens"] == 300
    assert body["store"] is False
    # The first text is dropped so the window starts on the homeowner's turn.
    assert body["input"] == [{"role": "user", "content": "How long does it take?"}]


def test_a_cut_off_answer_is_never_returned() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        body = openai_response_body(
            status="incomplete",
            text="The inspection takes",
            incomplete_details={"reason": "max_output_tokens"},
        )
        return httpx.Response(200, json=body)

    with pytest.raises(WriterError, match="max_output_tokens"):
        writer_with_fake_http(handler).write(facts="", history=[Turn("user", "hi")], reason="x")


def test_an_http_error_becomes_a_writer_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "boom", "type": "server_error"}})

    with pytest.raises(WriterError):
        writer_with_fake_http(handler).write(facts="", history=[Turn("user", "hi")], reason="x")


def test_the_facts_come_from_the_settings_and_the_conversation() -> None:
    facts = conversation_facts(SETTINGS, "12 Saguaro Ln, Mesa AZ", booked_label=None)
    assert "Inspections take about 60 minutes." in facts
    assert "Monday to Friday, between 8 AM and 5 PM, up to 14 days ahead" in facts
    assert "12 Saguaro Ln, Mesa AZ" in facts
    assert "No inspection has been booked yet" in facts
    assert "artificial intelligence that texts" in facts
    booked = conversation_facts(SETTINGS, "12 Saguaro Ln", booked_label="Tuesday October 6 at 8 AM")
    assert "An inspection is booked for Tuesday October 6 at 8 AM." in booked


# ---- the guard ----


def check_answers(**yes_probabilities: float) -> dict[str, Answer]:
    """Jev's answers to the reply checks: every check 0.0 unless given."""
    return {name: NoulAnswer(type="noul", noul=yes_probabilities.get(name, 0.0)) for name in REPLY_CHECKS}


CLEAN_DRAFT = "The inspection takes about an hour."


def test_a_clean_draft_passes() -> None:
    assert find_reply_problems(CLEAN_DRAFT, check_answers()) == []


@pytest.mark.parametrize("check_name", list(REPLY_CHECKS))
def test_each_jev_check_blocks_on_its_own(check_name: str) -> None:
    answers = check_answers(**{check_name: BLOCK_AT_PROBABILITY})
    assert find_reply_problems(CLEAN_DRAFT, answers) == [check_name]
    # Just under the line passes, so the threshold is the one in guard.py, not a stricter one.
    just_under = check_answers(**{check_name: BLOCK_AT_PROBABILITY - 0.01})
    assert find_reply_problems(CLEAN_DRAFT, just_under) == []


@pytest.mark.parametrize(
    "draft",
    [
        "Most homeowners insurance covers hail damage.",
        "Your deductible is usually around a thousand.",
        "We can help you file the claim.",
        "Our team works directly with your adjuster.",
        "That should be covered.",
    ],
)
def test_insurance_talk_is_blocked_even_when_jev_scores_it_low(draft: str) -> None:
    assert "insurance_words" in find_reply_problems(draft, check_answers())


@pytest.mark.parametrize(
    "draft",
    [
        "A repair is usually $400.",
        "Roofs run about 450 dollars per square.",
        "We offer 10% off for seniors.",
        "We have financing available.",
        "There's a small fee for the trip.",
    ],
)
def test_price_talk_is_blocked_even_when_jev_scores_it_low(draft: str) -> None:
    assert "price_words" in find_reply_problems(draft, check_answers())


def test_saying_the_inspection_is_free_is_allowed() -> None:
    assert find_reply_problems("The inspection is free, with no obligation.", check_answers()) == []


def test_empty_and_long_drafts_are_blocked() -> None:
    assert find_reply_problems("   ", check_answers()) == ["empty"]
    long_draft = "word " * (MAX_REPLY_CHARACTERS // 5 + 1)
    assert find_reply_problems(long_draft, check_answers()) == ["too_long"]


@pytest.mark.parametrize(
    "draft",
    [
        "I'll pass that question to the team, and the inspector can answer it at the visit.",
        "I can pass any questions I can't answer to the team.",
        "Someone will call you back about that.",
        "Our team will follow up with you on that.",
    ],
)
def test_a_follow_up_promised_in_the_writers_own_words_is_blocked(draft: str) -> None:
    assert "promises_follow_up" in find_reply_problems(draft, check_answers())
