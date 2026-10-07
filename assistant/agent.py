"""Deterministic scheduling workflow.

nlu.parse() says what the user *said*; this module decides what *happens*.
Every fact shown to the user comes from a SchedulingAPI response.
"""

import re
from datetime import datetime

from .api import ApiError, SchedulingAPI
from .nlu import parse
from .trace import trace

YES = re.compile(r"^\s*(yes|y|yeah|yep|confirm|book it)[.!]?\s*$", re.I)
NO = re.compile(r"^\s*(no|n|nope|cancel)[.!]?\s*$", re.I)
NICE = {"primary_care": "primary care", "dermatology": "dermatology"}
HELP = "I can help you find a provider, book an appointment, or look up your existing appointments."


def fmt_time(iso):
    return datetime.fromisoformat(iso).strftime("%a %b %d, %I:%M %p")


class Agent:
    def __init__(self, api=None):
        self.api = api or SchedulingAPI()
        self.patient = None  # set only after exactly one verified match
        self.names = {}      # providerId -> name, cached from /providers
        self.reset()

    def reset(self):
        """Clear the current task (a verified patient stays verified)."""
        self.intent = None
        self.phone = self.dob = None
        self.id_attempts = 0
        self.candidates = []      # multiple matches awaiting zip
        self.specialty = self.location = None
        self.location_asked = False
        self.slots = []           # slots exactly as returned by /availability
        self.pending = None       # slot awaiting yes/no
        self.issue = None         # last unresolved problem, used as handoff reason

    def state(self):
        if self.pending:
            return "confirming"
        if self.slots:
            return "choosing_slot"
        if self.candidates:
            return "disambiguating"
        return "verified" if self.patient else "start"

    def handle(self, text):
        p = parse(text)
        trace("turn", intent=p["intent"], state=self.state(),
              specialty=p.get("specialty"), location=p.get("location"))
        try:
            return self._route(text, p)
        except ApiError as err:
            if err.status >= 500:
                return self.handoff("api_failure", "Our scheduling system isn't responding right now, "
                                    "so I can't look anything up or book anything.")
            trace("api_rejected", code=err.code)
            return self.handoff("other", "The scheduling system rejected that request ({}).".format(err.code))

    # --- routing --------------------------------------------------------

    def _route(self, text, p):
        # Safety guards first, in every state.
        if p["intent"] == "medical_advice":
            return self.handoff("medical_advice", "I'm not able to give medical advice. If this could be an "
                                "emergency, call 911 or go to the nearest emergency room now.")
        if p["intent"] == "human":
            return self.handoff(self.issue or "user_requested", "Sure, I'll connect you with our staff.")
        if "unsupported" in (p.get("specialty"), p.get("location")):
            return self.handoff("unsupported_request", "We can only schedule primary care or dermatology "
                                "at downtown, uptown or lakeside.")

        if p["intent"] in ("provider_lookup", "book", "my_appointments") and p["intent"] != self.intent:
            self.intent, self.slots, self.pending = p["intent"], [], None
        if p.get("specialty"):
            self.specialty = p["specialty"]
        if p.get("location"):
            self.location = p["location"]

        # Booking: confirmation is a strict yes/no, never inferred by the model.
        if self.pending:
            if YES.match(text):
                return self._book()
            if NO.match(text):
                self.pending = None
                return "No problem, nothing was booked.\n" + self._slot_menu()
            return "Please reply 'yes' to book or 'no' to choose another option."
        if self.slots and not (p.get("specialty") or p.get("location")):
            n = p.get("slot_number")
            if n and 1 <= n <= len(self.slots):
                self.pending = self.slots[n - 1]
                return "Book {}? (yes/no)".format(self._describe(self.pending))
            return "Please choose a number from 1 to {}.".format(len(self.slots))

        if self.intent == "provider_lookup":
            return self._providers()
        if self.intent in ("book", "my_appointments"):
            ask = self._identify(p)
            if ask:
                return ask
            return self._availability() if self.intent == "book" else self._my_appointments()
        return HELP

    # --- identity -------------------------------------------------------

    def _identify(self, p):
        """Returns a question / handoff message, or None once one patient is verified."""
        if self.patient:
            return None
        if self.candidates:
            if not p.get("zip"):
                return "What is the 5-digit zip code on file?"
            match = [c for c in self.candidates if c["zipCode"] == p["zip"]]
            self.candidates = []
            if len(match) != 1:
                return self.handoff("identity_unclear", "I still can't confirm which record is yours.")
            self.patient = match[0]
            return None

        self.phone = p.get("phone") or self.phone
        self.dob = p.get("dob") or self.dob
        if not (self.phone and self.dob):
            return ("To do that I need to verify your identity. Please give the phone number "
                    "(e.g. 555-0101) and date of birth (YYYY-MM-DD) on file.")
        matches = self.api.search_patients(self.phone, self.dob)
        self.phone = self.dob = None  # don't keep identifiers around longer than needed
        trace("identity", matches=len(matches))
        if not matches:
            self.id_attempts += 1
            if self.id_attempts >= 2:
                return self.handoff("identity_unclear", "I still couldn't find a matching patient record.")
            return "I couldn't find a patient with those details. Please double-check the phone and date of birth."
        if len(matches) > 1:
            # Reveal nothing about the matches; ask for the field that tells them apart.
            self.candidates = matches
            return "I found more than one record with those details. What is the 5-digit zip code on file?"
        self.patient = matches[0]
        return None

    # --- workflows ------------------------------------------------------

    def _providers(self):
        loc = None if self.location == "any" else self.location
        found = self.api.providers(self.specialty, loc)
        self.intent = None  # one-shot: a later "thanks" must not re-run the lookup
        what = " ".join(x for x in (NICE.get(self.specialty), "providers", loc and "at " + loc) if x)
        if not found:
            return "I couldn't find any {}.".format(what)
        lines = ["- {} ({}; {}; {})".format(p["name"], NICE.get(p["specialty"], p["specialty"]),
                                             ", ".join(p["locations"]), ", ".join(p["modalities"]))
                 for p in found]
        return "Here are the {}:\n{}".format(what, "\n".join(lines))

    def _availability(self):
        hello = "Thanks, {}. ".format(self.patient["firstName"])
        if not self.specialty:
            return hello + "Which specialty: primary care or dermatology?"
        if not self.location and not self.location_asked:
            self.location_asked = True
            return hello + "Which location: downtown, uptown or lakeside (or 'any')?"
        loc = None if self.location == "any" else self.location
        self.slots = self.api.availability(self.patient["patientId"], self.specialty, loc)
        trace("availability", count=len(self.slots))
        if not self.slots:
            self.issue = "no_availability"
            return ("There are no open {} appointments{}. You can try another location, "
                    "or say 'human' and our staff will follow up.").format(
                        NICE[self.specialty], " at " + loc if loc else "")
        return self._slot_menu()

    def _slot_menu(self):
        lines = ["{}. {}".format(i, self._describe(s)) for i, s in enumerate(self.slots, 1)]
        return "Available appointments:\n{}\nReply with a number.".format("\n".join(lines))

    def _describe(self, slot):
        return "{} with {} at {}".format(fmt_time(slot["startTime"]),
                                         self._name(slot["providerId"]),
                                         slot["location"])

    def _book(self):
        slot, self.pending = self.pending, None
        try:
            appt = self.api.book(self.patient["patientId"], slot["slotId"])
        except ApiError as err:
            if err.status != 409:
                raise
            trace("booking", outcome="slot_taken")
            self.slots = [s for s in self.slots if s["slotId"] != slot["slotId"]]
            if not self.slots:
                return self.handoff("no_availability", "Sorry, that slot was just taken and nothing else is open.")
            return "Sorry, that slot was just taken by someone else, so it was NOT booked.\n" + self._slot_menu()
        trace("booking", outcome="booked")
        self.reset()
        return "Booked! Confirmation {}: {} with {} at {}.".format(
            appt["appointmentId"], fmt_time(appt["startTime"]), self._name(appt["providerId"]), appt["location"])

    def _name(self, provider_id):
        if provider_id not in self.names:
            self.names = {p["providerId"]: p["name"] for p in self.api.providers()}
        return self.names.get(provider_id, provider_id)

    def _my_appointments(self):
        appts = self.api.appointments(self.patient["patientId"])
        self.reset()
        if not appts:
            return "You have no appointments on file."
        lines = ["- {}: {} with {} at {} ({})".format(a["appointmentId"], fmt_time(a["startTime"]),
                                                     self._name(a["providerId"]), a["location"], a["status"])
                 for a in appts]
        return "Your appointments:\n" + "\n".join(lines)

    # --- handoff --------------------------------------------------------

    def handoff(self, reason, message):
        """Create a human handoff. Never claims success if the API call failed."""
        summary = "reason={} intent={} specialty={} location={} state={}".format(
            reason, self.intent, self.specialty, self.location, self.state())
        patient_id = self.patient["patientId"] if self.patient else None
        try:
            ticket = self.api.handoff(reason, summary, patient_id)
            trace("handoff", reason=reason, created=True)
            tail = " I've passed this to our scheduling staff (reference {}).".format(ticket)
        except ApiError:
            trace("handoff", reason=reason, created=False)
            tail = " I also couldn't create a follow-up request, so please call the clinic directly."
        self.reset()
        return message + tail
