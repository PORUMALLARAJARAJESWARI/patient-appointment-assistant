"""Thin client for the mock scheduling API (stdlib only)."""

import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .trace import trace

BASE_URL = os.environ.get("SCHEDULING_API_URL", "http://localhost:4010")


class ApiError(Exception):
    def __init__(self, status, code, message=""):
        super().__init__(message or code)
        self.status, self.code = status, code


class SchedulingAPI:
    def __init__(self, base_url=BASE_URL, outage=False):
        self.base_url = base_url
        # Forces the mock's 503 path on every request (demo / tests only).
        self.headers = {"Content-Type": "application/json"}
        if outage:
            self.headers["X-Mock-Scenario"] = "api_failure"

    def _call(self, method, path, query=None, body=None):
        url = self.base_url + path + ("?" + urlencode(query) if query else "")
        data = json.dumps(body).encode() if body is not None else None
        started = time.time()
        status = None
        try:
            with urlopen(Request(url, data=data, method=method, headers=self.headers), timeout=5) as resp:
                status = resp.status
                return json.loads(resp.read())
        except HTTPError as err:
            status = err.code
            payload = json.loads(err.read() or b"{}")
            raise ApiError(err.code, payload.get("code", "error"), payload.get("message", ""))
        except (URLError, OSError) as err:
            status = 0  # connection refused / timeout -> treat like an outage
            raise ApiError(503, "unreachable", str(err))
        finally:
            # Path only: the query string carries phone/DOB and must not be logged.
            trace("api_call", method=method, path=path, status=status,
                  ms=round((time.time() - started) * 1000, 1))

    def search_patients(self, phone, dob):
        return self._call("GET", "/patients/search", {"phone": phone, "dob": dob})["matches"]

    def appointments(self, patient_id):
        return self._call("GET", "/patients/{}/appointments".format(patient_id))["appointments"]

    def providers(self, specialty=None, location=None):
        query = {k: v for k, v in {"specialty": specialty, "location": location}.items() if v}
        return self._call("GET", "/providers", query)["providers"]

    def availability(self, patient_id, specialty, location=None):
        query = {"patientId": patient_id, "specialty": specialty}
        if location:
            query["location"] = location
        return self._call("GET", "/availability", query)["slots"]

    def book(self, patient_id, slot_id):
        # confirmed=True is only ever sent from Agent after an explicit "yes".
        body = {"patientId": patient_id, "slotId": slot_id, "confirmed": True}
        return self._call("POST", "/appointments", body=body)["appointment"]

    def handoff(self, reason, summary, patient_id=None):
        body = {"reason": reason, "summary": summary, "patientId": patient_id}
        return self._call("POST", "/handoffs", body=body)["handoffId"]
