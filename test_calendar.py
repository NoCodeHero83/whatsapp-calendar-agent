from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime, timedelta

# =====================
# CONFIG
# =====================

SERVICE_ACCOUNT_FILE = "whatsapp-calendar-agent-496013-c12f3ed285d4.json"

SCOPES = ["https://www.googleapis.com/auth/calendar"]

CALENDAR_ID = "c_24afc5e613eb1431994665b3ecdc26129efc54d851ce1a378b136e28ead4f961@group.calendar.google.com"

# =====================
# AUTH
# =====================

credentials = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE,
    scopes=SCOPES
)

service = build("calendar", "v3", credentials=credentials)

# =====================
# EVENTO DE PRUEBA
# =====================

start_time = datetime.utcnow() + timedelta(minutes=5)
end_time = start_time + timedelta(hours=1)

event = {
    "summary": "TEST DEMO - Taller IA",
    "description": "Evento creado por service account",
    "start": {
        "dateTime": start_time.isoformat() + "Z",
        "timeZone": "America/Lima",
    },
    "end": {
        "dateTime": end_time.isoformat() + "Z",
        "timeZone": "America/Lima",
    },
}

# =====================
# CREAR EVENTO
# =====================

event_result = service.events().insert(
    calendarId=CALENDAR_ID,
    body=event
).execute()

print("EVENT CREATED:")
print(event_result.get("htmlLink"))