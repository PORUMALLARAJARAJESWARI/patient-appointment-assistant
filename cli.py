#!/usr/bin/env python3
"""Interactive CLI for the scheduling assistant.

    python3 cli.py            # normal
    python3 cli.py --outage   # sends X-Mock-Scenario: api_failure to demo the outage path
"""

import argparse
import os

from assistant.agent import Agent
from assistant.api import SchedulingAPI


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--outage", action="store_true", help="simulate the scheduling API being down")
    args = parser.parse_args()

    agent = Agent(SchedulingAPI(outage=args.outage))
    base_url = os.environ.get("OPENAI_BASE_URL")
    mode = ("LLM at " + base_url if base_url else "OpenAI" if os.environ.get("OPENAI_API_KEY")
            else "offline keyword parser")
    print("Scheduling assistant ({}). Type 'quit' to exit.".format(mode))
    print("Assistant: Hi! How can I help? I can find providers, book appointments, or look up yours.")
    while True:
        try:
            text = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in ("quit", "exit"):
            break
        if text:
            print("Assistant:", agent.handle(text))


if __name__ == "__main__":
    main()
