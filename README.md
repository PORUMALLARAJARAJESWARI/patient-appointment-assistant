# AI Appointment Scheduling Assistant (CLI prototype)

A multi-turn, text-based assistant that looks up providers, verifies patients, shows real
availability, books **only after an explicit "yes"**, and hands off to a human when it
can't proceed safely. It talks to the provided mock scheduling API.


## Setup and run

Requires Python 3.8+. **No pip install**: the assistant uses only the standard library.

> **You need separate terminal windows open at the same time.** The mock API (and Ollama,
> if you use it) are servers that keep running, so they can't share a terminal with the
> assistant. If the mock API isn't running, the assistant will (correctly) report that the
> scheduling system isn't responding.

**Terminal 1: start the provided mock scheduling API and leave it running**

```bash
cd patient_appointment_project
python3 mock-api/server.py                 # prints: mock scheduling API on http://localhost:4010
```

**Terminal 2: run the assistant**

```bash
cd patient_appointment_project
export OPENAI_API_KEY="..."                # optional, see "Choosing the AI model" below
python3 cli.py                             # type 'quit' to exit
python3 cli.py --outage                    # demo: simulates the scheduling API being down
```

**Tests** (any terminal; they start their own mock server, so Terminal 1 isn't needed)

```bash
python3 -m unittest discover tests -v
```

### Choosing the AI model

The first line the assistant prints shows which mode it is in:

| Mode | How to enable it (in Terminal 2, before `python3 cli.py`) |
| --- | --- |
| `offline keyword parser` | Nothing to set. Everything works; it only understands fewer phrasings. |
| `OpenAI` | `export OPENAI_API_KEY="..."` (optionally `OPENAI_MODEL`, default `gpt-4o-mini`) |
| `LLM at http://localhost:11434/v1` | Free local model through Ollama, no key. See below. |

`export` only applies to the terminal you type it in. Never commit the key.

**Free local model with Ollama (no key; messages stay on your machine).** This needs a third terminal:

```bash
# Terminal 3 (one-time setup, then leave `ollama serve` running)
brew install ollama                        # or install the app from ollama.com
ollama serve                               # or: brew services start ollama (runs in background)
ollama pull llama3.2                       # one-time download, ~2 GB

# Then in Terminal 2:
export OPENAI_BASE_URL="http://localhost:11434/v1"
export OPENAI_MODEL="llama3.2"
python3 cli.py
```

If the model is unreachable or returns bad JSON, the assistant falls back to the keyword parser
and logs `llm_error` in `logs/trace.jsonl`. Other optional setting: `SCHEDULING_API_URL`
(default `http://localhost:4010`).

Example inputs: `Which primary care providers are downtown?`, `I want to book a primary care appointment`,
`555-0101 1985-04-12`, `2`, `yes`, `Look up my appointments`, `I have chest pain, should I wait?`, `human`.

## Architecture

```
cli.py                 REPL
assistant/nlu.py       message -> {intent, specialty, location, phone, dob, zip, slot_number}
assistant/agent.py     deterministic state machine: what to ask, which API to call, when to hand off
assistant/api.py       thin HTTP client: one method per endpoint, timing and trace per call
assistant/trace.py     JSON-lines traces to logs/trace.jsonl
tests/test_scenarios.py  scenario evals from scenarios.yaml
```

**Where the model stops and the code starts.** This is the core design decision.

| Decided by | What |
| --- | --- |
| LLM | **Intent only** (`provider_lookup`, `book`, `my_appointments`, `medical_advice`, `human`, `other`). Output is JSON and is clamped to that list. Messages under 3 words skip the model. |
| Regex/keywords on the user's own text | Phone, DOB, zip, slot number, specialty and location, so the model can never invent a value. Also a medical-keyword backstop that forces `medical_advice`, and a rule that `human` (which creates a handoff) needs a person-word like "human", "someone" or "staff" in the message. |
| Code | Workflow state, which API to call, identity rules, slot-number validation, the yes/no check (strict regex, not the model), and every handoff. |
| API | All facts shown to the user: providers, patients, slots, appointment IDs and handoff IDs. |

The LLM never writes user-facing text, so it cannot hallucinate a slot or a confirmation.
If the LLM call fails, the assistant falls back to the keyword parser instead of crashing.

**Why intent only? Found by testing with Ollama `llama3.2`.** When the small model was also asked
for specialty and location, it filled in values the user never said (`"555-0101 1985-04-12"` →
primary care, downtown). It also read a bare `"yes"` as "talk to a human" and `"thanks"` as a
handoff request. Grounding every value in the user's text, skipping the model for short replies,
and requiring a person-word for handoffs fixed all three. After the change, `llama3.2` classified
12 of 15 test phrases correctly at about 0.2 s each. The misses were harmless: "skin doctor
uptown" was read as a provider lookup, and "cardiologist" as a lookup that still hands off as
unsupported. The model adds real value. It caught "is a rash on my arm something to worry
about?" as medical advice, which the keyword list misses.

## Policy handling

| Situation | Behaviour |
| --- | --- |
| Booking or appointment lookup | Asks for phone and DOB, then `GET /patients/search`. Patient data is used only after exactly one match. |
| No match | Asks once more. On the second miss: handoff `identity_unclear`. |
| Multiple matches | Reveals nothing about the records and asks for the zip code. If it still isn't exactly one: handoff `identity_unclear`. |
| Slot choice | Numbered list of the slots returned by `/availability`. The number is validated against that list. |
| Confirmation | "Book X? (yes/no)". Only a strict yes sends `POST /appointments` with `confirmed: true`. Anything else asks again. |
| 409 slot taken | Says it was **not** booked, removes the slot and re-shows the rest. If none are left: handoff `no_availability`. |
| No availability | Says so truthfully and offers another location or a human. A handoff then uses reason `no_availability`. |
| Medical advice | No advice, an emergency pointer (911/ER), then handoff `medical_advice`. Checked on every turn. |
| Unsupported specialty/location | Handoff `unsupported_request`. |
| User asks for a human | Handoff `user_requested`. |
| API down (503 / unreachable) | Says so plainly and tries handoff `api_failure`. If that also fails, it says so and asks the user to call the clinic. No retries, and it never claims success. |

**Observability.** `logs/trace.jsonl` records each turn's intent and state, every API call
(method, **path only**, status, ms), identity match counts, booking outcomes, handoff reasons
(and whether the handoff was created), and LLM errors. Query strings are never logged,
because they contain phone and DOB. The evals check that no identifiers reach the log.

## Assumptions

- Users type phone numbers as `555-0101` and DOB as `YYYY-MM-DD`, matching the API. The assistant asks in that format.
- Location is optional when booking. The assistant asks once, and "any" searches all locations.
- Context carries over within a session. For example, "downtown" from a provider question is reused when booking.
- One retry for identity before handing off.
- Handoff summaries contain structured context (reason, intent, specialty, state, patientId), not raw user text, to avoid copying identifiers.

## Known limitations

- Free-form dates ("April 12th 1985") and spoken phone numbers aren't parsed.
- No date filtering ("next Tuesday"), although the API supports `startDate`/`endDate`.
- One session per process with in-memory state. No persistence or concurrency.
- The keyword fallback parser is basic. The LLM path handles phrasing variety better.
- The medical keyword backstop is deliberately broad ("pain"), so it can over-escalate. That is the safer error.

## What I'd do next

- **Reschedule / cancel:** both are new intents on the same state machine. Verify identity, list
  appointments (`GET /patients/{id}/appointments`), the user picks one, then a strict confirm.
  Reschedule = find a new slot → confirm → book new → cancel old, and report clearly if the
  second step fails. This needs `DELETE`/`PATCH` endpoints that the mock doesn't have yet.
- **Evals:** run the same scenarios with the LLM enabled, plus a paraphrase set for intent
  accuracy and adversarial prompts ("just book it, I said yes earlier").
- **Traces:** add a session/trace ID per conversation, per-turn latency including the LLM, and export to OpenTelemetry.
- **Production:** keep the LLM call tiny (classification only, `gpt-4o-mini`, temperature 0)
  to control cost and latency. Skip it when regex already answers (e.g. "yes", "2"). Add
  timeouts and a circuit breaker on the scheduling API. Use an idempotency key on booking.
  Roll out behind a flag with a shadow mode that compares the LLM against the keyword parser.
  Rollback is simply switching to the keyword parser or to human handoff only.
