"""
WhatsApp → OpenAI → Lead Qualification webhook.
Handles Meta webhook verification (GET) and stateful lead qualification (POST).

Hardening:
- POST always returns HTTP 200 immediately; processing runs in a background thread.
- WhatsApp message IDs are deduplicated via SQLite to survive Meta retries.
- Conversation state is persisted in SQLite and survives server restarts.
- Google Calendar remains connected but is NOT used during qualification.
"""

import os
import sys
import json
from pathlib import Path
from datetime import datetime

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

from fastapi import FastAPI, Request, Response, BackgroundTasks

from services.openai_service import process_lead_message
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

app = FastAPI(title="WhatsApp Calendar Agent — Lead Qualification Mode")

_verify_token_loaded = bool(os.environ.get("META_VERIFY_TOKEN"))
logger.info(f"META_VERIFY_TOKEN loaded: {_verify_token_loaded}")


# ── Phase field requirements ──────────────────────────────────────────────────

PHASE_FIELDS = {
    1: ["lead_name", "property_type"],
    2: ["usage_intent", "urgency"],
    3: {
        "terrain": ["zone", "min_area_m2", "needs_services", "needs_title"],
        "other": ["zone", "bedrooms_or_area", "essential_feature"],
    },
    4: ["budget_range", "payment_method"],
    5: ["decision_maker", "available_for_visit"],
}


def _get_phase_fields(phase: int, is_terrain: bool = False) -> list[str]:
    if phase == 3:
        return PHASE_FIELDS[3]["terrain"] if is_terrain else PHASE_FIELDS[3]["other"]
    return PHASE_FIELDS.get(phase, [])


def _compute_current_phase(lead_data: dict, is_terrain: bool) -> int:
    """Determine the earliest incomplete phase (1-5). Returns 6 if all complete."""
    for phase in range(1, 6):
        fields = _get_phase_fields(phase, is_terrain)
        missing = [f for f in fields if not lead_data.get(f)]
        if missing:
            return phase
    return 6


# ── Lead evaluation ───────────────────────────────────────────────────────────

def _evaluate_lead(lead_data: dict) -> tuple[bool, int, dict]:
    """Evaluate lead against 7 criteria. Returns (is_qualified, score, detail)."""
    criteria = {
        "intencion_clara": bool(lead_data.get("usage_intent")),
        "urgencia": lead_data.get("urgency") in ("alta", "media"),
        "zona_definida": bool(lead_data.get("zone")),
        "presupuesto_real": bool(lead_data.get("budget_range")),
        "capacidad_pago": bool(lead_data.get("payment_method")),
        "tomador_decision": bool(lead_data.get("decision_maker")),
        "disponibilidad": (
            lead_data.get("available_for_visit") is True
            or bool(lead_data.get("contact_info"))
        ),
    }

    score = sum(1 for v in criteria.values() if v)
    is_qualified = score >= 5
    return is_qualified, score, criteria


def _determine_rejection_reason(lead_data: dict) -> str:
    """Return the letter (A-D) for the primary rejection reason."""
    if lead_data.get("urgency") == "baja" or not lead_data.get("urgency"):
        return "A"
    if not lead_data.get("budget_range"):
        return "B"
    if not lead_data.get("decision_maker"):
        return "C"
    return "D"


def _send_qualified_closing(phone: str, lead_data: dict) -> None:
    """Send closing message for qualified leads and log structured summary."""
    name = lead_data.get("lead_name", "estimado/a")
    contact = lead_data.get("contact_info") or phone

    message = (
        f"¡Muchas gracias, {name}! Con la información que me has dado, "
        f"creo que tenemos opciones que pueden interesarte mucho. Voy a pasar "
        f"tu consulta a uno de nuestros asesores especializados, quien se "
        f"comunicará contigo a la brevedad al {contact}. ¡Que tengas un excelente día!"
    )
    send_message(phone, message)

    summary = (
        f"LEAD CALIFICADO — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Nombre: {lead_data.get('lead_name', 'N/A')}\n"
        f"Teléfono: {phone}\n"
        f"Tipo de propiedad: {lead_data.get('property_type', 'N/A')}\n"
        f"Uso previsto: {lead_data.get('usage_intent', 'N/A')}\n"
        f"Urgencia: {lead_data.get('urgency', 'N/A')}\n"
        f"Zona de interés: {lead_data.get('zone', 'N/A')}\n"
        f"Presupuesto: {lead_data.get('budget_range', 'N/A')}\n"
        f"Forma de pago: {lead_data.get('payment_method', 'N/A')}\n"
        f"Tomador de decisión: {lead_data.get('decision_maker', 'N/A')}\n"
        f"Disponibilidad para visita: {'Sí' if lead_data.get('available_for_visit') else 'No informado'}\n"
        f"Criterios cumplidos: X / 7\n"
        f"Notas adicionales: {lead_data.get('urgency_comment', 'N/A')}"
    )
    logger.info(f"\n{'='*60}\n{summary}\n{'='*60}")


def _send_non_qualified_closing(phone: str, lead_data: dict, reason: str) -> None:
    """Send closing message for non-qualified leads."""
    name = lead_data.get("lead_name", "estimado/a")

    messages = {
        "A": (
            f"Entendido, {name}. Cuando estés más cerca de tomar la decisión, "
            f"con gusto te ayudamos a encontrar la opción ideal. Mientras tanto, "
            f"¿te gustaría que te mantengamos informado sobre nuevas propiedades "
            f"disponibles que se ajusten a lo que buscas?"
        ),
        "B": (
            f"Entendemos tu situación, {name}. Por el momento no contamos con "
            f"opciones dentro de ese rango, pero nuestro portafolio cambia "
            f"constantemente. ¿Podemos incluirte en nuestra lista de novedades "
            f"para avisarte cuando tengamos algo que se ajuste?"
        ),
        "C": (
            f"Perfecto, {name}. Cuando puedas conversar con "
            f"{lead_data.get('decision_involves', 'la persona que decide')}, "
            f"con gusto agendamos una llamada con los dos para explicarles "
            f"todo con detalle. ¿Cuándo sería un buen momento?"
        ),
        "D": (
            f"Sin problema, {name}. Si en algún momento decides avanzar o "
            f"tienes alguna duda, aquí estaremos. ¿Hay algo más en lo que te "
            f"pueda ayudar por ahora?"
        ),
    }

    send_message(phone, messages.get(reason, messages["D"]))


# ── Core message processing — multi-turn lead qualification ──────────────────

def _process_message(phone_number: str, text: str, message_id: str | None) -> None:
    # ── Idempotency check ─────────────────────────────────────────────────────
    if message_id:
        if is_message_processed(message_id):
            logger.info(f"Duplicate message ignored — id={message_id} phone={phone_number}")
            return
        mark_message_processed(message_id)

    try:
        logger.info(f"Processing message — phone={phone_number} id={message_id} text={text!r}")

        # 1. Load accumulated state for this user
        state = get_state(phone_number)

        # 2. If already evaluated, ignore new messages (or restart)
        if state.stage == "done":
            logger.info(f"Lead already evaluated for {phone_number} — ignoring")
            return

        # 3. Process message through OpenAI — get response + extracted data
        result = process_lead_message(
            message_text=text,
            accumulated_data=state.data,
            current_phase=state.phase,
            retry_count=state.retry_count,
            is_terrain=state.is_terrain,
        )

        agent_response = result["agent_response"]
        extracted = result["extracted"]

        # 4. Merge extracted data into accumulated state
        merged = merge_data(state.data, extracted)
        merged["phone"] = phone_number

        # 5. Detect if property_type was newly set to "terreno"
        if not state.is_terrain and merged.get("property_type") == "terreno":
            state.is_terrain = True
            logger.info(f"Terrain flow activated for {phone_number}")

        # 6. Determine current phase from accumulated data
        new_phase = _compute_current_phase(merged, state.is_terrain)

        # 7. Track retries: if phase hasn't advanced and no new data, increment
        phase_advanced = new_phase > state.phase
        had_new_data = any(
            extracted.get(k) is not None
            for k in _get_phase_fields(state.phase, state.is_terrain)
        )

        if not phase_advanced and not had_new_data:
            state.retry_count += 1
            logger.info(f"Retry increment for {phone_number} — count={state.retry_count}")
        elif phase_advanced:
            state.retry_count = 0

        # 8. If retry limit reached (2), skip current phase fields
        if state.retry_count >= 2:
            logger.info(f"Retry limit reached for {phone_number} — skipping phase {state.phase}")
            state.retry_count = 0

        # 9. Save updated state
        state.data = merged
        state.phase = new_phase if new_phase <= 5 else 5

        # 10. Check if all 5 phases are complete → evaluate
        if new_phase >= 6:
            logger.info(f"All phases complete for {phone_number} — evaluating lead")
            is_qualified, score, criteria = _evaluate_lead(merged)

            if is_qualified:
                _send_qualified_closing(phone_number, merged)
            else:
                reason = _determine_rejection_reason(merged)
                _send_non_qualified_closing(phone_number, merged, reason)

            state.stage = "done"
            save_state(phone_number, state)
            clear_state(phone_number)
            logger.info(
                f"Lead evaluation complete — phone={phone_number} "
                f"qualified={is_qualified} score={score}/7"
            )
            return

        # 11. Still qualifying — send AI-generated response
        save_state(phone_number, state)
        send_message(phone_number, agent_response)

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
