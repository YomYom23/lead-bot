# lead-bot/src/lead_bot/call_script.py
"""Everything the bot says on a phone call that is fixed in advance, and the instructions Twilio
runs to connect a call to us.

The calls are incoming: the business's own line forwards calls nobody picked up to the Twilio
number. The caller rang the business, so the opening of an automated call the bot places (the
press-9-to-stop line the federal robocall rule requires) doesn't apply. The greeting still says
it's an automated assistant and that the call is transcribed: the caller expected a person, and
some states require everyone on a call to know it's being recorded or transcribed. Twilio speaks
the greeting itself (welcomeGreeting below) before our server hears a word.
"""

from __future__ import annotations

import html
import re

from .settings import BusinessSettings
from .text_script import plain_punctuation

# Fixed answers to the questions the bot must not answer itself.
#   Insurance: in roofing, talking coverage, deductibles or "we'll handle your claim" counts as
#   acting as an unlicensed public adjuster in several states. The bot only points to the insurer.
#   Price: no price list exists to quote from, and a made-up number is the worst thing it could say.
INSURANCE_LINE = (
    "I can't help with insurance questions. Your insurance company is the right one to ask, "
    "and our inspector will document what they find."
)
PRICE_LINE = (
    "I can't give prices over the phone. The inspector will give you a written estimate after the inspection."
)
QUESTION_FOR_TEAM_LINE = "I'll pass that question to our team so the inspector can answer it."

# Said the moment an off-script question is recognised, before the writer drafts an answer. The
# writer and the guard take a second or two, and without this the caller hears dead air.
CHECKING_LINE = "Let me check."

# Asked after a new caller books. The exact words are stored as the consent they agreed to.
TEXT_CONSENT_QUESTION = "Can I text the booking details to the number you're calling from?"


def greeting(settings: BusinessSettings) -> str:
    return (
        f"Thanks for calling {settings.business_name}. Sorry we missed you. I'm the automated "
        "assistant, and this call is transcribed. I can book a free roof inspection or take a "
        "message. What can I do for you?"
    )


def booking_confirmation_text(settings: BusinessSettings, slot_label: str, address: str) -> str:
    """The text a caller who agreed gets after booking. The address is in it because speech
    recognition gets addresses wrong, and a wrong one is cheaper to catch now than on the day."""
    return plain_punctuation(
        f"{settings.business_name}: your free roof inspection is booked for {slot_label} at "
        f"{address}. If the address isn't right, reply here and we'll fix it. Reply STOP to opt out."
    )


# ---- what the caller said, tidied ----

# "My name is Dana Ruiz." -> "Dana Ruiz". Only phrases at the very start are dropped.
NAME_LEAD_INS = re.compile(
    r"^\s*(?:hi|hello|yeah|yes|sure|ok(?:ay)?)?[,.!\s]*(?:my name is|my name's|"
    r"this is|it's|it is|i'm|i am|name's)\s+",
    re.IGNORECASE,
)
ADDRESS_LEAD_INS = re.compile(
    r"^\s*(?:yeah|yes|sure|ok(?:ay)?)?[,.!\s]*(?:the address is|address is|"
    r"it's|it is|i'm at|we're at|i live at|we live at)\s+",
    re.IGNORECASE,
)


def spoken_name(words: str) -> str:
    """The caller's name from what they said, as well as speech-to-text heard it."""
    name = NAME_LEAD_INS.sub("", words).strip().rstrip(".!?,").strip()
    # A transcript in all lower case reads oddly in the owner's email and the calendar.
    return name.title() if name.islower() else name


def spoken_address(words: str) -> str:
    return ADDRESS_LEAD_INS.sub("", words).strip().rstrip(".!?,").strip()


def same_first_name(spoken: str, full_name_on_file: str) -> bool:
    """Whether the name the caller gave includes the first name on the lead. Loose on purpose:
    "Dana" and "Dana Ruiz" both match "Dana Ruiz", while a different person's name doesn't."""
    first_names = full_name_on_file.split()
    if not first_names:
        return False
    return re.search(rf"\b{re.escape(first_names[0])}\b", spoken, re.IGNORECASE) is not None


# ---- phone numbers, read aloud ----


def spoken_digits(e164_phone: str) -> str:
    """'+1' then 4805550199 -> '4 8 0, 5 5 5, 0 1 9 9'.

    Spaced digits make a text-to-speech voice read them one at a time. '480-555-0199' can come
    out as "four hundred eighty".
    """
    digits = e164_phone.removeprefix("+1")
    return f"{' '.join(digits[:3])}, {' '.join(digits[3:6])}, {' '.join(digits[6:])}"


# A US phone number written the usual ways: 480-555-0199, (480) 555-0199, 480.555.0199, and with a
# leading +1 or 1. The writer rewrites numbers in its own style, so its drafts are converted.
WRITTEN_PHONE_NUMBER = re.compile(r"(?:\+?1[-. ]?)?\(?\b(\d{3})\)?[-. ]?(\d{3})[-. ](\d{4})\b")


def speakable_phone_numbers(text: str) -> str:
    """Every written phone number in `text`, rewritten digit by digit as spoken_digits does."""
    return WRITTEN_PHONE_NUMBER.sub(lambda match: spoken_digits("+1" + "".join(match.groups())), text)


# ---- the instructions for Twilio ----


def call_twiml(settings: BusinessSettings, call_id: int, relay_token: str) -> str:
    """The TwiML for one incoming call: Twilio speaks the greeting, then connects the live
    conversation to our relay (relay.py).

    Attribute choices, from Twilio's ConversationRelay docs:
      welcomeGreetingInterruptible="any"  a caller who starts talking over the greeting is
                                          heard; they rang the business and may just say why
      interruptible="any"                 the same for everything the bot says after it
    The call id and its one-time token travel as <Parameter>s and come back in the setup
    message, where store.connect_call checks them. Nothing about the caller goes in the TwiML.
    """
    relay_attributes = {
        "url": settings.relay_url,
        "welcomeGreeting": greeting(settings),
        "welcomeGreetingInterruptible": "any",
        "interruptible": "any",
        "language": "en-US",
    }
    parameters = {"call_id": str(call_id), "relay_token": relay_token}
    # html.escape with quote=True escapes & < > " and ', which covers XML attribute values. A
    # business name like "Smith & Sons" would otherwise break the document.
    attribute_text = " ".join(
        f'{name}="{html.escape(value, quote=True)}"' for name, value in relay_attributes.items()
    )
    parameter_text = "".join(
        f'<Parameter name="{html.escape(name, quote=True)}" value="{html.escape(value, quote=True)}"/>'
        for name, value in parameters.items()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Connect><ConversationRelay {attribute_text}>{parameter_text}</ConversationRelay>"
        "</Connect><Hangup/></Response>"
    )
