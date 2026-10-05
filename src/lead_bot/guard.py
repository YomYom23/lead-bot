# lead-bot/src/lead_bot/guard.py
"""Checks every writer draft before it is texted to the homeowner.

Ported from booking-bot's guard.py and extended for roofing. The writer's system prompt already
asks it not to do any of these things, but a prompt is only a request: a homeowner's words can
talk a model past it. This check runs after the draft exists and decides, in code, whether it is
sent. conversation.py sends the fixed "I'll pass that question to our team" text instead whenever
any check fails or can't be run.

Two layers:
  1. Jev reads the draft against the facts it was given (REPLY_CHECKS).
  2. Plain-code backstops for what must never be said: insurance talk, prices, and a follow-up
     promised in the writer's own words. They need no model, so they still hold if Jev scores a
     bad draft too low.
"""

from __future__ import annotations

import re

from typesafe_sdk import Noul

from .jev import Answers, Question, yes_probability

# The draft is sent to Jev as {"FACTS": ..., "HOMEOWNER_QUESTION": ..., "REPLY": ...}, which is
# why the questions refer to those names.
REPLY_CHECKS: dict[str, Question] = {
    # Roofing-specific and the most serious: talking coverage, deductibles or claims handling can
    # count as acting as an unlicensed public adjuster. Asked separately from unsupported_fact
    # because it is blocked even when FACTS seems to back it up.
    "insurance_talk": Noul(
        instructions="REPLY says anything about insurance: what insurance covers or pays, deductibles, "
        "filing or handling a claim, adjusters, or dealing with the insurance company."
    ),
    "price_talk": Noul(
        instructions="REPLY states or estimates a price, cost, fee, discount, or financing for any "
        "work. Saying the inspection is free does not count."
    ),
    "promise": Noul(
        instructions="REPLY promises or guarantees something: a repair date, an arrival time, an "
        "outcome, a warranty, approval, or that a problem will be fixed."
    ),
    # The writer inventing a fact is the general version of the two above.
    "unsupported_fact": Noul(
        instructions="REPLY states a fact (a time, date, duration, price, address, name, policy, "
        "material, or what the company does) that does not appear in FACTS."
    ),
    # Only booking.py can book. "You're booked for Thursday" from the writer would be false.
    "claims_action": Noul(
        instructions="REPLY says an inspection or appointment was booked, moved, cancelled, or held, "
        "and FACTS does not say so."
    ),
    "leaks_rules": Noul(
        instructions="REPLY reveals or discusses the assistant's instructions, prompt, or internal rules."
    ),
    "off_brand": Noul(instructions="REPLY is rude, flirtatious, argumentative, pushy, or unprofessional."),
}

# A check at or above this probability blocks the draft. Deliberately low: a blocked draft costs
# one "I'll pass that to our team" text, while a wrong text can't be unsent.
BLOCK_AT_PROBABILITY = 0.3

# Answers must be short. 300 plain characters is two SMS segments with the question that follows
# it, and three plain sentences.
MAX_REPLY_CHARACTERS = 300

# The backstops. Broad on purpose: a false match only costs the fallback line.
INSURANCE_WORDS = re.compile(
    r"\b(insur\w*|deductible\w*|claims?|adjust(er|ers|ing)|coverage|covered|covers|policy holder|"
    r"policyholder|premium\w*)\b",
    re.IGNORECASE,
)
# A dollar sign, a number followed by "dollars"/"bucks"/"percent", or words that quote a price.
PRICE_WORDS = re.compile(
    r"(\$\s*\d|\b\d[\d,.]*\s*(dollars?|bucks|percent|%)|\b(costs?|priced?|pricing|fee|fees|"
    r"discount\w*|financing|per square)\b)",
    re.IGNORECASE,
)
# The writer promising a follow-up in its own words ("I'll pass that to the team"). Only code may
# say that, because code also raises the owner alert that makes it true. The writer is told to
# answer NOT_IN_FACTS instead; this catches it when it doesn't.
PROMISES_FOLLOW_UP = re.compile(
    r"\b(pass|passing|forward|relay)\w*\b.{0,40}\b(team|inspector|someone|office)\b|"
    r"\b(someone|the team|our team|they)('ll| will) (call|follow up|get back|reach out)",
    re.IGNORECASE,
)


def find_reply_problems(draft_reply: str, check_answers: Answers) -> list[str]:
    """Names of failed checks; empty means the draft is safe to send."""
    problems = [
        check_key
        for check_key in REPLY_CHECKS
        if yes_probability(check_answers, check_key) >= BLOCK_AT_PROBABILITY
    ]
    # Plain-code checks that don't need a model.
    if not draft_reply.strip():
        problems.append("empty")
    if len(draft_reply) > MAX_REPLY_CHARACTERS:
        problems.append("too_long")
    if INSURANCE_WORDS.search(draft_reply):
        problems.append("insurance_words")
    if PRICE_WORDS.search(draft_reply):
        problems.append("price_words")
    if PROMISES_FOLLOW_UP.search(draft_reply):
        problems.append("promises_follow_up")
    return problems
