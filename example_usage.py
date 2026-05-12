"""
Example: create a Google Calendar event with a Meet link via Domain-Wide Delegation.
Run: python example_usage.py
"""

import os
from dotenv import load_dotenv
load_dotenv()

from services.calendar_service import create_event

result = create_event(
    summary="Test Meeting - WhatsApp Agent",
    start_datetime="2026-05-20T10:00:00-05:00",
    end_datetime="2026-05-20T11:00:00-05:00",
)

print("Event ID  :", result["id"])
print("Event Link:", result["link"])
