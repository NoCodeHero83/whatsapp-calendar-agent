import os
import json
from datetime import date, timedelta
from typing import Optional
from openai import OpenAI

from prompts.extraction_prompt import build_prompt
from utils.logger import get_logger

logger = get_logger(__name__)

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    return _client

# Structured output schema for appointment extraction
EXTRACTION_SCHEMA = {
    "name": "appointment_extraction",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "customer_name": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": "Nombre completo del cliente",
            },
            "vehicle": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": "Marca y modelo del vehículo (ej. 'Toyota Corolla')",
            },
            "plate": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": "Placa del vehículo en mayúsculas sin espacios (ej. 'ABC123')",
            },
            "service_type": {
                "anyOf": [
                    {
                        "type": "string",
                        "enum": ["preventive_maintenance", "technical_diagnostics"],
                    },
                    {"type": "null"},
                ],
                "description": "Tipo de servicio: preventive_maintenance o technical_diagnostics",
            },
            "appointment_date": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": "Fecha de la cita en formato YYYY-MM-DD",
            },
            "start_time": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": "Hora de inicio en formato HH:MM (24h)",
            },
            "estimated_end_time": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": "Hora de fin estimada en formato HH:MM (24h)",
            },
            "phone": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": "Número de teléfono del cliente si se menciona",
            },
            "notes": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": "Notas adicionales relevantes",
            },
        },
        "required": [
            "customer_name",
            "vehicle",
            "plate",
            "service_type",
            "appointment_date",
            "start_time",
            "estimated_end_time",
            "phone",
            "notes",
        ],
        "additionalProperties": False,
    },
}


def extract_appointment_info(message_text: str) -> dict:
    """
    Use OpenAI structured outputs to extract appointment data from a free-form
    Spanish WhatsApp message sent by a mechanic.
    Returns a dict with all schema fields (values may be None if not found).
    """
    today = date.today()
    tomorrow = today + timedelta(days=1)
    day_after = today + timedelta(days=2)

    system_prompt = build_prompt(
        today=today.strftime("%Y-%m-%d"),
        tomorrow=tomorrow.strftime("%Y-%m-%d"),
        day_after_tomorrow=day_after.strftime("%Y-%m-%d"),
    )

    logger.info(f"Extracting appointment info from: {message_text!r}")

    try:
        response = _get_client().chat.completions.create(
            model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message_text},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": EXTRACTION_SCHEMA,
            },
            temperature=0,  # Deterministic for data extraction
        )

        raw = response.choices[0].message.content
        appointment = json.loads(raw)
        logger.info(f"Extracted appointment data: {appointment}")
        return appointment

    except Exception as e:
        logger.error(f"OpenAI extraction failed: {e}", exc_info=True)
        raise


def get_missing_required_fields(appointment: dict) -> list[str]:
    """Return list of human-readable Spanish field names that are missing."""
    required = {
        "customer_name": "nombre del cliente",
        "vehicle": "vehículo",
        "plate": "placa",
        "service_type": "tipo de servicio",
        "appointment_date": "fecha",
        "start_time": "hora",
    }

    missing = []
    for field, label in required.items():
        if not appointment.get(field):
            missing.append(label)

    return missing
