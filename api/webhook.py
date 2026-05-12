"""
WhatsApp → OpenAI → Google Calendar webhook.
Handles Meta webhook verification (GET) and stateful appointment booking (POST).

Hardening:
- POST always returns HTTP 200 immediately; processing runs in a background thread.
- WhatsApp message IDs are deduplicated via SQLite to survive Meta retries.
- Conversation state is persisted in SQLite and survives server restarts.
- Calendar events are tagged with the sender's phone to prevent duplicate creation.
"""

import os
import sys
import json
import asyncio
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

from fastapi import FastAPI, Request, Response, BackgroundTasks

from services.openai_service import extract_appointment_info
from services.calendar_service import (
    check_availability,
    create_appointment,
    find_existing_appointment,
    SERVICE_DISPLAY_NAMES,
)
from services.whatsapp_service import send_message
from services.state_service import (
    get_state,
    save_state,
    clear_state,
    merge_data,
    is_message_processed,
    mark_message_processed,
)
from utils.logger import get_logger

logger = get_logger(__name__)

app = FastAPI(title="WhatsApp Calendar Agent")

_verify_token_loaded = bool(os.environ.get("META_VERIFY_TOKEN"))
logger.info(f"META_VERIFY_TOKEN loaded: {_verify_token_loaded}")


# ── Conversation constants ────────────────────────────────────────────────────

REQUIRED_FIELDS = [
    "customer_name",
    "vehicle",
    "plate",
    "service_type",
    "appointment_date",
    "start_time",
]

FOLLOW_UP_QUESTIONS = {
    "customer_name": "¿Cuál es tu nombre completo? 👤",
    "vehicle": "¿Qué vehículo tienes? Por ejemplo: Toyota Corolla 🚗",
    "plate": "¿Me puedes pasar la placa del vehículo? 🔢",
    "service_type": (
        "¿Qué servicio necesitas? 🔧\n"
        "1️⃣ Mantenimiento preventivo\n"
        "2️⃣ Diagnóstico técnico"
    ),
    "appointment_date": "¿Para qué fecha quieres la cita? 📅 (ej. mañana, lunes, 15 de mayo)",
    "start_time": "¿A qué hora prefieres la cita? 🕐 (ej. 10am, 3:30pm)",
}


# ── Reply builders ────────────────────────────────────────────────────────────

def _build_confirmation_reply(appointment: dict, event: dict) -> str:
    service_display = SERVICE_DISPLAY_NAMES.get(
        appointment.get("service_type", ""),
        appointment.get("service_type", "Servicio"),
    )

    lines = [
        "✅ ¡Cita creada correctamente!",
        "",
        f"📅 Fecha: {appointment['appointment_date']}",
        f"🕐 Hora: {appointment['start_time']}",
        f"🔧 Servicio: {service_display}",
        f"👤 Cliente: {appointment['customer_name']}",
        f"🚗 Vehículo: {appointment.get('vehicle', 'N/A')} — Placa: {appointment.get('plate', 'N/A')}",
    ]

    calendar_link = event.get("htmlLink")
    meet_link = event.get("hangoutLink")

    if calendar_link:
        lines.append(f"\n📆 Calendario: {calendar_link}")
    if meet_link:
        lines.append(f"📹 Meet: {meet_link}")

    return "\n".join(lines)


# ── Core message processing — stateful, multi-turn ───────────────────────────

def _process_message(phone_number: str, text: str, message_id: str | None) -> None:
    # ── Idempotency check ─────────────────────────────────────────────────────
    if message_id:
        if is_message_processed(message_id):
            logger.info(f"Duplicate message ignored — id={message_id} phone={phone_number}")
            return
        mark_message_processed(message_id)

    try:
        logger.info(f"Processing message — phone={phone_number} id={message_id} text={text!r}")

        # 1. Load accumulated data for this user
        state = get_state(phone_number)

        # 2. Extract whatever OpenAI can find in this new message
        newly_extracted = extract_appointment_info(text)

        # 3. Merge into accumulated state (non-None values win)
        merged = merge_data(state.data, newly_extracted)

        # 4. Tag with the WhatsApp sender phone so calendar can detect duplicates
        merged["whatsapp_phone"] = phone_number

        # 5. Find the first required field that is still missing
        missing = [f for f in REQUIRED_FIELDS if not merged.get(f)]

        if missing:
            state.data = merged
            state.stage = "collecting_info"
            save_state(phone_number, state)

            next_field = missing[0]
            logger.info(f"Missing fields for {phone_number}: {missing} — asking for {next_field!r}")
            send_message(phone_number, FOLLOW_UP_QUESTIONS[next_field])
            return

        # 6. All required fields collected
        logger.info(f"All fields collected for {phone_number} — checking for existing appointment")
        state.stage = "ready_to_create"

        # Check for an existing event for this phone+time BEFORE the general
        # availability check. If the user already has a booking at this slot,
        # check_availability would return False (slot busy) and wrongly ask for
        # a new time, so we short-circuit here instead.
        existing_event = find_existing_appointment(merged)
        if existing_event:
            logger.info(f"Existing appointment confirmed for {phone_number} — sending confirmation")
            reply = _build_confirmation_reply(merged, existing_event)
            send_message(phone_number, reply)
            clear_state(phone_number)
            return

        logger.info(f"No existing event — checking general calendar availability")
        is_available = check_availability(merged)

        if not is_available:
            merged["appointment_date"] = None
            merged["start_time"] = None
            state.data = merged
            state.stage = "collecting_info"
            save_state(phone_number, state)
            logger.info(f"Slot busy for {phone_number} — asking for alternative time")
            send_message(
                phone_number,
                "⚠️ Ya hay una cita en ese horario. ¿Qué otro horario te queda bien? 📅",
            )
            return

        # 7. Create the calendar event (also duplicate-safe inside create_appointment)
        event = create_appointment(merged)
        logger.info(f"Calendar event ready for {phone_number} — id={event.get('id')}")

        reply = _build_confirmation_reply(merged, event)
        send_message(phone_number, reply)

        # 8. Conversation complete — reset state
        clear_state(phone_number)

    except Exception as e:
        logger.error(
            f"Unhandled error processing message from {phone_number}: {e}",
            exc_info=True,
        )
        send_message(
            phone_number,
            "❌ Ocurrió un error al procesar tu mensaje. Por favor intenta nuevamente.",
        )


# ── Webhook payload parser ────────────────────────────────────────────────────

def _parse_incoming_message(data: dict) -> tuple[str | None, str | None, str | None]:
    """Return (phone_number, text, message_id) from a Meta webhook payload."""
    try:
        entry = data.get("entry", [])
        if not entry:
            return None, None, None

        changes = entry[0].get("changes", [])
        if not changes:
            return None, None, None

        value = changes[0].get("value", {})
        messages = value.get("messages", [])

        if not messages:
            return None, None, None

        message = messages[0]

        if message.get("type") != "text":
            logger.info(f"Ignoring non-text message type: {message.get('type')!r}")
            return None, None, None

        phone_number = message.get("from")
        text = message.get("text", {}).get("body", "").strip()
        message_id = message.get("id")

        if not phone_number or not text:
            return None, None, None

        return phone_number, text, message_id

    except Exception as e:
        logger.error(f"Error parsing webhook payload: {e}", exc_info=True)
        return None, None, None


# ── FastAPI routes ────────────────────────────────────────────────────────────

@app.get("/api/webhook")
async def verify_webhook(request: Request) -> Response:
    params = dict(request.query_params)
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    verify_token = os.environ.get("META_VERIFY_TOKEN", "")

    if mode == "subscribe" and token == verify_token and challenge:
        logger.info("Meta webhook verification successful")
        return Response(content=challenge, media_type="text/plain")

    logger.warning(
        f"Webhook verification failed — mode={mode!r}, token_match={token == verify_token}"
    )
    return Response(
        content=json.dumps({"error": "Forbidden"}),
        status_code=403,
        media_type="application/json",
    )


@app.post("/api/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks) -> Response:
    """
    Always returns HTTP 200 immediately.
    Message processing runs as a background task AFTER the response is sent,
    so Meta never times out waiting for our downstream API calls.
    """
    raw_body = await request.body()
    ok = Response(content=json.dumps({"status": "ok"}), media_type="application/json")

    if not raw_body:
        return ok

    try:
        data = json.loads(raw_body)
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in webhook body: {e}")
        return ok

    logger.debug(f"Webhook payload: {json.dumps(data)}")

    phone_number, text, message_id = _parse_incoming_message(data)

    if phone_number and text:
        logger.info(f"Message received — phone={phone_number} id={message_id}")
        background_tasks.add_task(_process_message, phone_number, text, message_id)
    else:
        logger.info("Webhook event has no processable text message — skipping")

    return ok
