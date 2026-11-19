# lead-bot/src/lead_bot/__init__.py
"""lead-agent: answers a roofing company's web leads by text message and books a free inspection.

A homeowner fills in the inspection form (server.py). If they agreed to texts, the worker
(worker.py) texts them within a minute, inside legal texting hours (timing.py), and keeps a proof
record of the consent it relied on (consent.py, store.py). Each reply is handled by the
conversation logic (conversation.py), which books the inspection on Google Calendar.
"""
