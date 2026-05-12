import os
import time
import requests
from utils.logger import get_logger

logger = get_logger(__name__)

GRAPH_API_VERSION = "v20.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

_MAX_RETRIES = 2
_RETRY_DELAY = 1.5  # seconds between retries


def send_message(to_phone: str, text: str) -> bool:
    """
    Send a WhatsApp text message via Meta Cloud API v20.
    Retries up to 2 times on failure. Returns True on success, False if all attempts fail.
    Never raises — the webhook must never crash due to a WhatsApp send failure.
    """
    phone_number_id = os.environ.get("META_PHONE_NUMBER_ID")
    access_token = os.environ.get("META_ACCESS_TOKEN")

    if not phone_number_id or not access_token:
        logger.error("META_PHONE_NUMBER_ID or META_ACCESS_TOKEN not configured")
        return False

    if not to_phone or not text:
        logger.error(f"send_message called with empty to_phone={to_phone!r} or text={text!r}")
        return False

    url = f"{GRAPH_API_BASE}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": text},
    }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    for attempt in range(1, _MAX_RETRIES + 2):  # attempts 1, 2, 3
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=10)

            if response.ok:
                logger.info(f"WhatsApp sent to {to_phone} — status {response.status_code} (attempt {attempt})")
                return True

            logger.error(
                f"WhatsApp API error {response.status_code} sending to {to_phone} "
                f"(attempt {attempt}/{_MAX_RETRIES + 1}): {response.text}"
            )

        except Exception as e:
            logger.error(
                f"WhatsApp send exception for {to_phone} "
                f"(attempt {attempt}/{_MAX_RETRIES + 1}): {e}",
                exc_info=True,
            )

        if attempt <= _MAX_RETRIES:
            logger.info(f"Retrying WhatsApp send to {to_phone} in {_RETRY_DELAY}s...")
            time.sleep(_RETRY_DELAY)

    logger.error(f"All {_MAX_RETRIES + 1} WhatsApp send attempts failed for {to_phone}")
    return False
