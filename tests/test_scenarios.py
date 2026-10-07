"""Scenario evals from scenarios.yaml, run against a fresh mock server.

    python3 -m unittest discover tests -v

Uses the offline keyword parser so results are deterministic (no OpenAI calls).
"""

import os
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("OPENAI_BASE_URL", None)

from assistant.agent import Agent  # noqa: E402
from assistant.api import SchedulingAPI  # noqa: E402
from assistant.trace import LOG_FILE  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Scenarios(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        port = free_port()
        cls.url = "http://localhost:{}".format(port)
        cls.server = subprocess.Popen([sys.executable, str(ROOT / "mock-api/server.py"), "--port", str(port)],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):  # wait for the server to accept connections
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
                break
            except OSError:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        cls.server.wait()

    def agent(self, outage=False):
        return Agent(SchedulingAPI(self.url, outage=outage))

    def pick(self, agent, slot_id):
        """Reply with the menu number of a given slot (menu order is the API's order)."""
        ids = [s["slotId"] for s in agent.slots]
        return agent.handle(str(ids.index(slot_id) + 1))

    def test_provider_lookup(self):
        reply = self.agent().handle("Which primary care providers are downtown?")
        self.assertIn("Dr. Elena Brooks", reply)
        self.assertIn("Dr. Marcus King", reply)
        self.assertNotIn("Dr. Priya Shah", reply)

    def test_happy_path_booking(self):
        a = self.agent()
        self.assertIn("phone number", a.handle("I want to book a primary care appointment downtown"))
        self.assertIn("Available appointments", a.handle("555-0101 1985-04-12"))
        self.assertIn("(yes/no)", self.pick(a, "slot_4001"))
        self.assertIn("reply 'yes'", a.handle("maybe"))  # not a confirmation
        reply = a.handle("yes")
        self.assertIn("Booked! Confirmation appt_", reply)

    def test_no_patient_match(self):
        a = self.agent()
        a.handle("Book an appointment")
        reply = a.handle("555-9999 1990-01-01")
        self.assertIn("couldn't find", reply)
        reply = a.handle("555-9999 1990-01-01")
        self.assertIn("reference handoff_", reply)

    def test_multiple_patient_matches(self):
        a = self.agent()
        a.handle("Look up my appointments")
        reply = a.handle("555-0130 1978-09-22")
        self.assertIn("zip code", reply)
        self.assertNotIn("Avery", reply)  # nothing about either record leaks
        self.assertIn("no appointments", a.handle("70115"))

    def test_multiple_matches_wrong_zip_hands_off(self):
        a = self.agent()
        a.handle("Look up my appointments")
        a.handle("555-0130 1978-09-22")
        self.assertIn("reference handoff_", a.handle("99999"))

    def test_slot_conflict(self):
        a = self.agent()
        a.handle("Book primary care downtown")
        a.handle("555-0101 1985-04-12")
        self.pick(a, "slot_conflict_001")
        reply = a.handle("yes")
        self.assertIn("NOT booked", reply)
        self.assertNotIn("slot_conflict_001", [s["slotId"] for s in a.slots])

    def test_no_availability(self):
        a = self.agent()
        a.handle("Book a dermatology appointment at Lakeside")
        reply = a.handle("555-0102 1992-06-03")
        self.assertIn("no open dermatology appointments", reply)
        self.assertIn("reference handoff_", a.handle("human please"))

    def test_medical_advice(self):
        reply = self.agent().handle("I have chest pain, should I wait?")
        self.assertIn("not able to give medical advice", reply)
        self.assertIn("reference handoff_", reply)

    def test_api_failure(self):
        a = self.agent(outage=True)
        a.handle("Book primary care")
        reply = a.handle("555-0101 1985-04-12")
        self.assertIn("isn't responding", reply)
        self.assertIn("call the clinic", reply)  # handoff also failed; we don't pretend it worked

    def test_trace_has_no_identifiers(self):
        a = self.agent()
        a.handle("Book primary care")
        a.handle("555-0101 1985-04-12")
        log = LOG_FILE.read_text()
        self.assertNotIn("555-0101", log)
        self.assertNotIn("1985-04-12", log)


if __name__ == "__main__":
    unittest.main()
