import os
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from uuid import uuid4
from typing import Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build

from utils.logger import get_logger

logger = get_logger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _get_timezone() -> str:
    return os.environ.get("TIMEZONE", "America/Lima")

# Default service durations when not specified by OpenAI
SERVICE_DURATIONS = {
    "preventive_maintenance": 60,
    "technical_diagnostics": 90,
}

SERVICE_DISPLAY_NAMES = {
    "preventive_maintenance": "Mantenimiento Preventivo",
    "technical_diagnostics": "Diagnóstico Técnico",
}


def _get_calendar_service():
    """Build an authenticated Google Calendar service instance."""
    service_account_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")

    if service_account_json:
        # Production: credentials from environment variable (JSON string)
        info = json.loads(service_account_json)
        credentials = service_account.Credentials.from_service_account_info(
            info, scopes=SCOPES
        )
    else:
        # Local development: fall back to JSON file in project root
        file_path = os.environ.get(
            "GOOGLE_SERVICE_ACCOUNT_FILE",
            "whatsapp-calendar-agent-496013-c12f3ed285d4.json",
        )
        credentials = service_account.Credentials.from_service_account_file(
            file_path, scopes=SCOPES
        )

    return build("calendar", "v3", credentials=credentials)


def _parse_local_datetime(date_str: str, time_str: str) -> datetime:
    """
    Combine a YYYY-MM-DD date string and HH:MM time string into a timezone-aware
    datetime in the configured TIMEZONE.
    """
    tz = ZoneInfo(_get_timezone())
    dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    return dt.replace(tzinfo=tz)


def _resolve_end_time(
    appointment: dict,
    start_dt: datetime,
) -> datetime:
    """
    Return end datetime. Uses extracted estimated_end_time if available,
    otherwise falls back to service-type default duration.
    """
    tz = ZoneInfo(_get_timezone())

    if appointment.get("estimated_end_time"):
        end_time_str = appointment["estimated_end_time"]
        date_str = start_dt.strftime("%Y-%m-%d")
        end_dt = datetime.strptime(f"{date_str} {end_time_str}", "%Y-%m-%d %H:%M")
        end_dt = end_dt.replace(tzinfo=tz)
        # Handle edge case: if end is before start (crosses midnight), add 1 day
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)
        return end_dt

    minutes = SERVICE_DURATIONS.get(appointment.get("service_type", ""), 60)
    return start_dt + timedelta(minutes=minutes)


def check_availability(appointment: dict) -> bool:
    """
    Return True if the time slot is free, False if there is a scheduling conflict.
    Uses the Google Calendar FreeBusy API.
    """
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID")
    service = _get_calendar_service()

    start_dt = _parse_local_datetime(
        appointment["appointment_date"], appointment["start_time"]
    )
    end_dt = _resolve_end_time(appointment, start_dt)

    # FreeBusy requires RFC3339 strings
    time_min = start_dt.isoformat()
    time_max = end_dt.isoformat()

    logger.info(f"Checking availability: {time_min} → {time_max}")

    body = {
        "timeMin": time_min,
        "timeMax": time_max,
        "timeZone": _get_timezone(),
        "items": [{"id": calendar_id}],
    }

    result = service.freebusy().query(body=body).execute()
    busy_slots = result.get("calendars", {}).get(calendar_id, {}).get("busy", [])

    if busy_slots:
        logger.warning(f"Slot is busy: {busy_slots}")
        return False

    logger.info("Slot is available")
    return True


def find_existing_appointment(appointment: dict) -> Optional[dict]:
    """
    Return the first calendar event tagged with this sender's phone at this
    exact date/time window, or None if no duplicate exists.
    Uses a private extended property so the lookup is precise and fast.
    """
    phone = appointment.get("whatsapp_phone")
    if not phone:
        return None

    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID")
    service = _get_calendar_service()

    start_dt = _parse_local_datetime(
        appointment["appointment_date"], appointment["start_time"]
    )
    end_dt = _resolve_end_time(appointment, start_dt)

    logger.info(f"Checking for duplicate event — phone={phone} window={start_dt.isoformat()}→{end_dt.isoformat()}")

    result = (
        service.events()
        .list(
            calendarId=calendar_id,
            timeMin=start_dt.isoformat(),
            timeMax=end_dt.isoformat(),
            privateExtendedProperty=f"whatsapp_phone={phone}",
            singleEvents=True,
            maxResults=1,
        )
        .execute()
    )

    items = result.get("items", [])
    if items:
        logger.info(f"Duplicate event found — id={items[0].get('id')} skipping creation")
        return items[0]
    return None


def create_appointment(appointment: dict) -> Optional[dict]:
    """
    Create a Google Calendar event with a Google Meet link.
    Returns the created event dict (contains htmlLink and hangoutLink).
    If a duplicate event already exists for this phone+time, returns the existing event.
    """
    # Guard: skip creation if this appointment was already booked
    existing = find_existing_appointment(appointment)
    if existing:
        logger.info("Returning existing event — no duplicate created")
        return existing

    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID")
    service = _get_calendar_service()

    start_dt = _parse_local_datetime(
        appointment["appointment_date"], appointment["start_time"]
    )
    end_dt = _resolve_end_time(appointment, start_dt)

    service_display = SERVICE_DISPLAY_NAMES.get(
        appointment.get("service_type", ""), appointment.get("service_type", "Servicio")
    )
    customer_name = appointment.get("customer_name", "Cliente")

    title = f"{service_display} - {customer_name}"

    description_lines = [
        f"🚗 Vehículo: {appointment.get('vehicle', 'N/A')}",
        f"🔢 Placa: {appointment.get('plate', 'N/A')}",
        f"📱 Teléfono: {appointment.get('phone') or 'No registrado'}",
        f"📝 Notas: {appointment.get('notes') or 'Sin notas'}",
        "",
        "📊 Estado: Pendiente",
        "",
        "Cita creada automáticamente vía WhatsApp AI Agent.",
    ]
    description = "\n".join(description_lines)

    event_body = {
        "summary": title,
        "description": description,
        "start": {
            "dateTime": start_dt.isoformat(),
            "timeZone": _get_timezone(),
        },
        "end": {
            "dateTime": end_dt.isoformat(),
            "timeZone": _get_timezone(),
        },
        # Automatically generate a Google Meet link
        "conferenceData": {
            "createRequest": {
                "requestId": f"apt-{uuid4().hex}",
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "email", "minutes": 60},
                {"method": "popup", "minutes": 30},
            ],
        },
        # Private tag used by find_existing_appointment to detect duplicates
        "extendedProperties": {
            "private": {
                "whatsapp_phone": appointment.get("whatsapp_phone", ""),
            }
        },
    }

    # Add attendee email if phone contains an email (edge case), or skip
    attendees = []
    if appointment.get("phone") and "@" in str(appointment.get("phone", "")):
        attendees.append({"email": appointment["phone"]})
    if attendees:
        event_body["attendees"] = attendees

    logger.info(f"Creating event: {title} at {start_dt.isoformat()}")

    try:
        # conferenceDataVersion=1 triggers Meet link generation
        created_event = (
            service.events()
            .insert(
                calendarId=calendar_id,
                body=event_body,
                conferenceDataVersion=1,
                sendUpdates="all",  # Send Google Calendar email invitations
            )
            .execute()
        )

        logger.info(f"Event created: {created_event.get('htmlLink')}")
        logger.info(f"Meet link: {created_event.get('hangoutLink')}")
        return created_event

    except Exception as e:
        logger.error(f"Failed to create calendar event: {e}", exc_info=True)
        raise
