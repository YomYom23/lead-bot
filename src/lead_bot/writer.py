# lead-bot/src/lead_bot/writer.py
"""The only generative model in the bot: it drafts an answer when the homeowner asks a question
the call script has no fixed line for ("How long does the inspection take?").

Ported from booking-bot's writer.py, with a system prompt rewritten for a phone call. It is given
no tools, so it cannot book, cancel, or look anything up. The worst it can do is write a bad
draft, and guard.py checks every draft before call_session.py lets it be spoken.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

import openai
from openai.types.responses import EasyInputMessageParam, ResponseInputItemParam


@dataclass(frozen=True)
class Turn:
    """One thing said on the call. "user" is the homeowner, "assistant" is the bot."""

    role: Literal["user", "assistant"]
    text: str


class WriterError(Exception):
    """The model call failed. The call falls back to the fixed "our team will answer that" line."""


class Writer(Protocol):
    """Anything with this `write` method can be the writer.

    A Protocol, so classes don't inherit from it, they just need a matching method. That's how
    the tests and the --offline simulator swap in stand-ins.
    """

    def write(self, *, facts: str, history: Sequence[Turn], reason: str) -> str: ...


# {facts} and {reason} are filled in per call. The rules here are a request to the model; guard.py
# is what enforces them, because a homeowner's words can talk a model out of its prompt.
#
# NOT_IN_FACTS: the writer's way of saying "I can't answer that". The writer must not word the
# deferral itself: "I'll pass that to the team" spoken by the writer is a promise no code knows
# about, so nobody is alerted and nobody follows up. On this marker, call_session.py says the
# fixed line AND raises the owner alert, so the promise is always backed.
NOT_IN_FACTS = "NOT_IN_FACTS"
SYSTEM_PROMPT_TEMPLATE = """You are the AI phone assistant for a roofing company, on a call with a
homeowner who asked for a free roof inspection. They just asked a question. Answer it.

Rules:
- Use only the information in FACTS. If FACTS doesn't answer the question, reply with exactly
  NOT_IN_FACTS and nothing else. The call then tells the homeowner the team will follow up, and
  makes sure someone does.
- Never discuss insurance: not coverage, deductibles, claims, adjusters, or what insurance pays.
- Never give or estimate a price, cost, discount, or financing, except that the inspection is free.
- Never promise or guarantee anything: repair dates, outcomes, warranties, arrival times.
- Never say an inspection was booked, changed, or cancelled unless FACTS says it was booked.
- This is spoken aloud: one or two short sentences, plain words, no lists, no symbols, no links.
  Write phone numbers exactly as FACTS gives them.
- Don't ask a follow-up question; the call script asks the next question itself.
Why you are being asked: {reason}

FACTS:
{facts}"""

# On a call there's no long thread to keep, and the older turns are the script's fixed questions.
MAX_HISTORY_TURNS = 8


def recent_turns(history: Sequence[Turn], max_turns: int = MAX_HISTORY_TURNS) -> list[Turn]:
    """The last max_turns turns, trimmed so the list starts on a homeowner turn.

    A call's history starts with the bot's opening, and a model API conversation is expected to
    open with the user, so the leading bot turns are dropped.
    """
    turns = list(history[-max_turns:])
    while turns and turns[0].role != "user":
        turns.pop(0)
    return turns


class OpenAIWriter:
    """The writer backed by an OpenAI model (gpt-6-luna by default), through the Responses API."""

    def __init__(
        self, model: str, client: openai.OpenAI | None = None, timeout_seconds: float = 4.0
    ) -> None:
        self._model = model
        # Tests pass in a client with a fake HTTP layer; otherwise the SDK reads OPENAI_API_KEY.
        # max_retries=0: on a call the homeowner is already waiting after "Let me check." A
        # failed draft falls back to the fixed line at once, instead of the SDK retrying with
        # backoff while they listen to silence.
        self._client = client if client is not None else openai.OpenAI(max_retries=0)
        self._timeout_seconds = timeout_seconds

    def write(self, *, facts: str, history: Sequence[Turn], reason: str) -> str:
        conversation_input: list[EasyInputMessageParam] = [
            {"role": turn.role, "content": turn.text} for turn in recent_turns(history)
        ]
        try:
            response = self._client.responses.create(
                model=self._model,
                instructions=SYSTEM_PROMPT_TEMPLATE.format(facts=facts, reason=reason),
                # list is invariant, so mypy needs the wider element type spelled out here.
                input=list[ResponseInputItemParam](conversation_input),
                # gpt-6-luna reasons at "medium" effort by default, and reasoning tokens count
                # against max_output_tokens: the cap could be spent before any answer is written.
                # Reasoning also adds seconds of silence on the line.
                reasoning={"effort": "none"},
                max_output_tokens=300,  # a spoken answer is two sentences; this also caps cost
                # The bot never fetches a past response, so homeowners' calls aren't stored there.
                store=False,
                timeout=self._timeout_seconds,
            )
        except openai.APIError as error:
            raise WriterError(str(error)) from error

        # "incomplete" means the model stopped early (token cap or content filter). A sentence
        # cut off halfway must never be spoken, so it counts as a failure like any other.
        if response.status == "incomplete":
            stop_reason = response.incomplete_details.reason if response.incomplete_details else "unknown"
            raise WriterError(f"incomplete response: {stop_reason}")
        return response.output_text.strip()
