# lead-bot/src/lead_bot/jev.py
"""The only module that talks to TypeSafe's Jev model.

Everything else in the bot calls `Jev.ask()` and reads answers through the two helper functions
at the bottom. Keeping every Jev call in one file means an API change only needs fixing here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from typesafe_sdk import (
    Choice,
    ChoiceAnswer,
    Noul,
    NoulAnswer,
    RetryPolicy,
    Score,
    ScoreAnswer,
    TypeSafeClient,
    TypeSafeError,
)

# The three question types Jev understands, and the matching answer types it returns:
#   Noul   -> yes/no question        -> NoulAnswer   (probability that the answer is "yes")
#   Choice -> pick one option        -> ChoiceAnswer (chosen option + confidence)
#   Score  -> rate on ordered levels -> ScoreAnswer  (score + confidence)
Question = Noul | Choice | Score
Answer = NoulAnswer | ChoiceAnswer | ScoreAnswer

# Answers come back keyed by the same names we gave the questions, e.g. {"injection": NoulAnswer}.
# Mapping (read-only) instead of dict: code that reads answers promises not to modify them.
Answers = Mapping[str, Answer]


class JevError(Exception):
    """TypeSafe failed or answered incompletely. Callers hand off to the owner; they never guess."""


class Jev:
    def __init__(
        self, client: TypeSafeClient | None = None, timeout_seconds: float = 5.0, max_retries: int = 2
    ) -> None:
        # Tests pass in a client with a fake HTTP layer. In production the SDK builds its own
        # client, reading TYPESAFE_API_KEY from the environment and defaulting to jev-latest.
        # On a phone call every second of silence counts, so calls pass a short timeout and one
        # retry. The SDK's own default (two retries, up to 30 seconds in all) suits a DM bot.
        self._client = (
            client
            if client is not None
            else TypeSafeClient(
                timeout=timeout_seconds,
                retry=RetryPolicy(max_retries=max_retries, timeout=timeout_seconds * (max_retries + 1)),
            )
        )

    def ask(self, state: Any, questions: Mapping[str, Question]) -> dict[str, Answer]:
        """Send one piece of context (`state`) and a set of named questions; get named answers back."""
        try:
            response = self._client.system_one(state, questions)
        except TypeSafeError as error:
            # Turn every SDK failure (timeout, 500, bad key...) into one error type,
            # so callers only need a single `except JevError`.
            raise JevError(str(error)) from error

        # Every question we asked must have an answer. Without this check, a missing
        # "injection" answer would be read as probability 0.0 and the message treated as safe.
        missing_keys = questions.keys() - response.answers.keys()
        if missing_keys:
            raise JevError(f"missing answers: {sorted(missing_keys)}")
        return dict(response.answers)


def yes_probability(answers: Answers, question_key: str) -> float:
    """Probability that a yes/no (noul) question is true; 0.0 if it wasn't asked."""
    answer = answers.get(question_key)
    # isinstance also narrows the type for mypy, so `.noul` is known to exist.
    return answer.noul if isinstance(answer, NoulAnswer) else 0.0


def choice_answer(answers: Answers, question_key: str) -> ChoiceAnswer | None:
    """The answer to a pick-one question, or None if that question wasn't asked this turn."""
    answer = answers.get(question_key)
    return answer if isinstance(answer, ChoiceAnswer) else None
