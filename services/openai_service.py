import os
import json
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


AGENT_RESPONSE_SCHEMA = {
    "name": "lead_agent_response",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "agent_response": {
                "type": "string",
                "description": "Tu respuesta conversacional al lead. Una sola pregunta. Sigue el flujo de fases obligatorio.",
            },
            "extracted": {
                "type": "object",
                "properties": {
                    "lead_name": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "description": "Nombre completo del lead",
                    },
                    "property_type": {
                        "anyOf": [
                            {
                                "type": "string",
                                "enum": [
                                    "casa",
                                    "departamento",
                                    "terreno",
                                    "local_comercial",
                                    "otro",
                                ],
                            },
                            {"type": "null"},
                        ],
                        "description": "Tipo de propiedad que busca",
                    },
                    "usage_intent": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "description": "Para qué usará la propiedad: vivienda propia, inversión, negocio, otro",
                    },
                    "urgency": {
                        "anyOf": [
                            {
                                "type": "string",
                                "enum": ["alta", "media", "baja"],
                            },
                            {"type": "null"},
                        ],
                        "description": "Urgencia de compra: alta (<3 meses), media (3-6 meses), baja (>6 meses o explorando)",
                    },
                    "urgency_comment": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "description": "Plazo aproximado o comentario textual sobre urgencia",
                    },
                    "zone": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "description": "Zona, distrito o ubicación de interés",
                    },
                    "bedrooms_or_area": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "description": "Número de dormitorios o metraje deseado (para no-terrenos)",
                    },
                    "essential_feature": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "description": "Característica indispensable que debe tener la propiedad",
                    },
                    "min_area_m2": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "description": "Área mínima en metros cuadrados (para terrenos)",
                    },
                    "needs_services": {
                        "anyOf": [{"type": "boolean"}, {"type": "null"}],
                        "description": "Si necesita servicios básicos (agua, luz, desagüe) en el terreno",
                    },
                    "needs_title": {
                        "anyOf": [{"type": "boolean"}, {"type": "null"}],
                        "description": "Si requiere título de propiedad inscrito en Registros Públicos",
                    },
                    "budget_range": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "description": "Rango de presupuesto aproximado para la compra",
                    },
                    "payment_method": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "description": "Cómo planea financiar: recursos propios, crédito hipotecario, combinación",
                    },
                    "decision_maker": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "description": "Quién toma la decisión de compra",
                    },
                    "decision_involves": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "description": "Persona involucrada en la decisión: pareja, socio, familiar u otro",
                    },
                    "available_for_visit": {
                        "anyOf": [{"type": "boolean"}, {"type": "null"}],
                        "description": "Si está disponible para visitar una propiedad próximamente",
                    },
                    "contact_info": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "description": "Mejor número o momento para que un asesor lo contacte",
                    },
                    "phase_completed": {
                        "anyOf": [{"type": "integer"}, {"type": "null"}],
                        "description": "Número de fase completada (1-5) o null si ninguna se completó en este mensaje",
                    },
                    "asked_count": {
                        "anyOf": [{"type": "integer"}, {"type": "null"}],
                        "description": "Número de veces que se ha preguntado la misma pregunta al lead (1, 2). Usar para detectar evasivas.",
                    },
                },
                "required": [
                    "lead_name",
                    "property_type",
                    "usage_intent",
                    "urgency",
                    "urgency_comment",
                    "zone",
                    "bedrooms_or_area",
                    "essential_feature",
                    "min_area_m2",
                    "needs_services",
                    "needs_title",
                    "budget_range",
                    "payment_method",
                    "decision_maker",
                    "decision_involves",
                    "available_for_visit",
                    "contact_info",
                    "phase_completed",
                    "asked_count",
                ],
                "additionalProperties": False,
            },
        },
        "required": ["agent_response", "extracted"],
        "additionalProperties": False,
    },
}


def process_lead_message(
    message_text: str,
    accumulated_data: dict | None = None,
    current_phase: int = 1,
    retry_count: int = 0,
    is_terrain: bool = False,
) -> dict:
    """
    Process a WhatsApp message from a lead and return both a conversational
    response and extracted structured data.

    Args:
        message_text: The incoming WhatsApp message from the lead.
        accumulated_data: Previously accumulated lead data (or None).
        current_phase: Current qualification phase (1-5).
        retry_count: Number of times the current question has been asked.

    Returns:
        dict with keys: agent_response (str), extracted (dict)
    """
    system_prompt = build_prompt()

    accumulated_json = json.dumps(accumulated_data or {}, ensure_ascii=False)
    context = (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"DATOS ACUMULADOS DEL LEAD (hasta ahora):\n"
        f"{accumulated_json}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"FASE ACTUAL: {current_phase}\n"
        f"TIPO DE PROPIEDAD: {'terreno' if is_terrain else 'pendiente de determinar'}\n"
        f"INTENTOS EN PREGUNTA ACTUAL: {retry_count}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"MENSAJE DEL LEAD:\n{message_text}"
    )

    logger.info(
        f"Processing lead message — phase={current_phase} accumulated={accumulated_json}"
    )

    try:
        response = _get_client().chat.completions.create(
            model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": context},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": AGENT_RESPONSE_SCHEMA,
            },
            temperature=0.3,
        )

        raw = response.choices[0].message.content
        result = json.loads(raw)
        logger.info(f"Lead agent response: agent_response={result['agent_response'][:80]!r}")
        logger.debug(f"Full extracted data: {result['extracted']}")
        return result

    except Exception as e:
        logger.error(f"OpenAI lead processing failed: {e}", exc_info=True)
        raise


def get_missing_lead_fields(lead_data: dict, phase: int, is_terrain: bool) -> list[str]:
    """Return list of field names still needed for the current phase."""
    phase_fields = {
        1: ["lead_name", "property_type"],
        2: ["usage_intent", "urgency"],
        3: (
            ["zone", "min_area_m2", "needs_services", "needs_title"]
            if is_terrain
            else ["zone", "bedrooms_or_area", "essential_feature"]
        ),
        4: ["budget_range", "payment_method"],
        5: ["decision_maker", "available_for_visit"],
    }

    required = phase_fields.get(phase, [])
    return [f for f in required if not lead_data.get(f)]
