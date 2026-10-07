"""Turns one user message into structured fields.

The LLM only classifies the intent. Everything else (phone, DOB, zip, slot number,
specialty, location) comes from words actually in the user's text, so the model can
never invent a value. Testing with a small local model showed why: it filled in
specialties and locations the user never said, and read "yes" as "human".
Short replies ("yes", "2", "downtown", a phone number) skip the model entirely.

LLM is used when OPENAI_API_KEY is set (OpenAI) or OPENAI_BASE_URL is set
(any OpenAI-compatible server, e.g. Ollama at http://localhost:11434/v1, no key).
Otherwise the keyword parser below is used.
"""

import json
import os
import re
from urllib.request import Request, urlopen

from .trace import trace

INTENTS = ("provider_lookup", "book", "my_appointments", "medical_advice", "human", "other")
LOCATIONS = ("downtown", "uptown", "lakeside")

# A handoff is an action, so "human" needs one of these words, whatever the model says.
HUMAN = re.compile(r"\b(human|person|someone|somebody|agent|representative|staff|operator|real)\b", re.I)

# Backstop: these always route to medical_advice, whatever the model says.
MEDICAL = re.compile(r"\b(chest pain|should i (wait|take|go)|symptoms?|medications?|dosage|diagnos\w*|"
                     r"is (it|this) serious|fever|bleeding|hurts?|pain)\b", re.I)

PROMPT = """Classify the user's message to a clinic scheduling assistant. Return JSON {"intent": "<intent>"}.
Intents:
- provider_lookup: asks which doctors/providers there are ("who are the dermatologists uptown?")
- book: wants to book, schedule or see a doctor ("can I get in to see someone for my skin?")
- my_appointments: wants to see their existing appointments ("when is my next visit?")
- medical_advice: describes symptoms or asks a health question ("is a fever of 101 bad?")
- human: explicitly asks for a person, agent or staff ("let me talk to someone real")
- other: anything else, such as greetings or thanks"""


def _regex_fields(text):
    fields = {}
    if m := re.search(r"\b\d{3}-\d{4}\b", text):
        fields["phone"] = m.group()
    if m := re.search(r"\b\d{4}-\d{2}-\d{2}\b", text):
        fields["dob"] = m.group()
    if m := re.search(r"\b\d{5}\b", text):
        fields["zip"] = m.group()
    if m := re.fullmatch(r"\s*#?(\d{1,2})\s*", text):
        fields["slot_number"] = int(m.group(1))
    return fields


def _keyword_classify(text):
    """Keyword parser: always supplies specialty/location; supplies intent when no LLM is used."""
    t = text.lower()
    if HUMAN.search(t):
        intent = "human"
    elif re.search(r"\bmy appointments?\b|upcoming|look ?up my", t):
        intent = "my_appointments"
    elif re.search(r"\b(book|schedule|make an appointment)\b", t):
        intent = "book"
    elif re.search(r"\b(providers?|doctors?|who)\b", t):
        intent = "provider_lookup"
    else:
        intent = "other"
    specialty = ("primary_care" if re.search(r"primary|family|checkup|\bgp\b", t)
                 else "dermatology" if re.search(r"dermatolog|skin", t)
                 else "unsupported" if re.search(r"cardiolog|dentist|pediatric|orthop|neurolog", t)
                 else None)
    location = next((loc for loc in LOCATIONS if loc in t), None)
    if location is None and re.search(r"\b(any|anywhere)\b", t):
        location = "any"
    return {"intent": intent, "specialty": specialty, "location": location}


def _llm_classify(text, base_url, key):
    body = {
        "model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": PROMPT}, {"role": "user", "content": text}],
    }
    headers = {"Content-Type": "application/json"}
    if key:  # local servers like Ollama need no key
        headers["Authorization"] = "Bearer " + key
    req = Request(base_url.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(), headers=headers)
    with urlopen(req, timeout=30) as resp:  # local models can be slow on the first call
        out = json.loads(json.loads(resp.read())["choices"][0]["message"]["content"])
    # Never trust the model blindly: anything outside the known list becomes "other".
    return out.get("intent") if out.get("intent") in INTENTS else "other"


def parse(text):
    key = os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get("OPENAI_BASE_URL")
    fields = _keyword_classify(text)
    if (key or base_url) and len(text.split()) >= 3:
        try:
            fields["intent"] = _llm_classify(text, base_url or "https://api.openai.com/v1", key)
        except Exception as err:  # network / quota / bad JSON -> keep keyword intent, don't crash
            trace("llm_error", error=type(err).__name__)
    if fields["intent"] == "human" and not HUMAN.search(text):
        fields["intent"] = "other"
    if MEDICAL.search(text):
        fields["intent"] = "medical_advice"
    fields.update(_regex_fields(text))
    return fields
