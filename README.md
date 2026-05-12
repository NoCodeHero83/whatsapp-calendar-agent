# WhatsApp AI Appointment Scheduling Agent

Production-ready MVP for auto repair workshops. Mechanics send free-form Spanish WhatsApp messages; the system extracts appointment data with OpenAI, checks Google Calendar availability, creates events with Google Meet links, and replies with confirmation — all automatically.

---

## Architecture

```
Mechanic (WhatsApp)
       │
       ▼
Meta WhatsApp Cloud API
       │  POST webhook
       ▼
/api/webhook.py          ← Vercel serverless function
       │
       ├─► services/openai_service.py    → Extract appointment data (structured outputs)
       ├─► services/calendar_service.py  → FreeBusy check + event creation + Meet link
       └─► services/whatsapp_service.py  → Send reply to mechanic
```

### File structure

```
api/
  webhook.py             # GET verification + POST message handler
services/
  openai_service.py      # OpenAI structured extraction
  calendar_service.py    # Google Calendar FreeBusy + event creation
  whatsapp_service.py    # Meta WhatsApp Cloud API client
utils/
  logger.py              # Structured logging
prompts/
  extraction_prompt.py   # OpenAI system prompt with date normalization
requirements.txt
vercel.json
.env.example
```

---

## Setup

### 1. Clone and install dependencies (local dev)

```bash
pip install -r requirements.txt
```

### 2. Configure environment variables

```bash
cp .env.example .env
# Fill in all required values
```

### 3. Google Calendar — share with service account

1. Open Google Calendar → your target calendar → Settings → Share with specific people
2. Add the service account email (found in the JSON file under `client_email`)
3. Grant **Make changes to events** permission
4. Copy the Calendar ID into `GOOGLE_CALENDAR_ID`

### 4. Deploy to Vercel

```bash
vercel deploy
```

Set all environment variables in the Vercel dashboard (Project → Settings → Environment Variables).

For `GOOGLE_SERVICE_ACCOUNT_JSON`, paste the **entire contents** of the JSON file as a single string value.

### 5. Configure Meta webhook

In Meta Developer Console → WhatsApp → Configuration:

- **Webhook URL**: `https://your-project.vercel.app/api/webhook`
- **Verify Token**: same value as `META_VERIFY_TOKEN`
- **Subscribed fields**: `messages`

---

## Message Flow

```
Mechanic sends:   "mañana toyota corolla de juan placa abc123 mantenimiento 3pm"

OpenAI extracts:
  customer_name:    "Juan"
  vehicle:          "Toyota Corolla"
  plate:            "ABC123"
  service_type:     "preventive_maintenance"
  appointment_date: "2026-05-12"
  start_time:       "15:00"
  estimated_end_time: "16:00"

Calendar check:   slot available ✓

Event created:    "Mantenimiento Preventivo - Juan"
                  2026-05-12 15:00–16:00 America/Lima
                  + Google Meet link

Reply sent:
  ✅ Cita creada correctamente.

  📅 Fecha: 2026-05-12
  🕐 Hora: 15:00
  🔧 Servicio: Mantenimiento Preventivo
  👤 Cliente: Juan
  🚗 Vehículo: Toyota Corolla — Placa: ABC123

  📆 Calendario: https://calendar.google.com/...
  📹 Meet: https://meet.google.com/...
```

### Example messages the system understands

| Message | What's extracted |
|---------|-----------------|
| `mañana 3pm entra el toyota de juan placa abc123 mantenimiento` | Full appointment, creates immediately |
| `agenda diagnóstico técnico para el kia de pedro el jueves 10am` | Missing plate → asks for it |
| `mañana revisar frenos corolla ATF223 cliente Luis 4pm` | Maps "frenos" → technical_diagnostics |
| `mañana toyota juan 3pm` | Missing plate + service → `⚠️ Faltan los siguientes datos: placa y tipo de servicio.` |

### Conflict handling

```
⚠️ Ya existe una cita en ese horario. Por favor elige otro horario.
```

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `META_VERIFY_TOKEN` | ✅ | Custom token for webhook verification |
| `META_ACCESS_TOKEN` | ✅ | Meta permanent access token |
| `META_PHONE_NUMBER_ID` | ✅ | WhatsApp phone number ID |
| `OPENAI_API_KEY` | ✅ | OpenAI API key |
| `GOOGLE_CALENDAR_ID` | ✅ | Target Google Calendar ID |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | ✅ (prod) | Full service account JSON as string |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | local only | Path to JSON file (dev fallback) |
| `TIMEZONE` | optional | IANA timezone, default `America/Lima` |
| `OPENAI_MODEL` | optional | Default `gpt-4o` |
| `DEBUG` | optional | Set to `true` for verbose logging |

---

## Tech Stack

- **Python 3.9+**
- **Vercel** — serverless Python functions (`@vercel/python`)
- **Meta WhatsApp Cloud API** — inbound webhooks + outbound messages
- **OpenAI API** — structured outputs (`gpt-4o`) for Spanish NLP extraction
- **Google Calendar API** — FreeBusy checks, event creation, Meet link generation

---

## Limitations (MVP scope)

- No database — Google Calendar is the source of truth
- No multi-turn conversation memory (each message is independent)
- No user authentication or dashboard
- Inbound-only — does not send proactive messages or templates
