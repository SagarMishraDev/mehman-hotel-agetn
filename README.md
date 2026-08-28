# Mehman.io — Hotel Booking AI Agent (Mehman.io Assignment)

A simplified version of Mehman.io, a guest-facing AI agent that understands natural language requests, maintains conversation state, calls deterministic tools for search/availability/pricing, stays grounded in real data, handles booking upgrades, and moves the conversation toward a booking — across web, WhatsApp, and Telegram.

## Quick Start

```bash
cd backend
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# fill in OPENAI_API_KEY (default provider) -- see Environment Variables below
uvicorn main:app --reload --port 8000
```

Open **http://localhost:8000** — the backend serves the frontend directly, no separate server needed.
Admin dashboard (all conversations across all channels): **http://localhost:8000/admin**

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `MODEL_PROVIDER` | No (defaults to `groq`) | `groq` (free, recommended), `claude`, or `gemini` |
| `GROQ_API_KEY` | If using Groq | Free, no card needed — console.groq.com/keys |
| `ANTHROPIC_API_KEY` | If using Claude | console.anthropic.com |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | If using Gemini | aistudio.google.com/apikey |
| `SMTP_USER` / `SMTP_PASSWORD` / `HOTEL_STAFF_EMAIL` | Only for email notifications | Gmail + App Password |
| `TELEGRAM_BOT_TOKEN` | Only for the Telegram channel | From @BotFather |

## Architecture

```
Guest (Web browser / WhatsApp / Telegram)
      |
      v
Channel adapter:
  - Web:      main.py  /chat
  - WhatsApp: main.py  /whatsapp  (Twilio webhook)
  - Telegram: telegram_bot.py     (polling, no public URL needed)
      |
      v
llm_agent.run_agent_turn(session_id, message)   <-- single shared agent core
      |
      +--> db.py (SQLite: guest state + conversation history + booking holds)
      |
      +--> LLM (Groq by default) with 9 tools defined
      |         |
      |         v (model calls a tool)
      |    tools.py  <-- deterministic Python functions, not the model
      |         |
      |         v
      |    hotel_data.json  <-- source of truth (3 properties)
      |
      +--> notify.py -- emails hotel staff on new/updated booking holds
      |
      v
Reply -> guest (on whichever channel they messaged from)
      +
Admin dashboard (frontend/admin.html) shows every conversation, live, across all channels
```

**Agent loop**: each guest message can trigger several tool calls in sequence (e.g. update state → search → check availability) before a final natural-language reply. Every tool call and result is captured in a `trace`, shown in the UI's debug panel. On the last allowed loop iteration, tool access is disabled so the model is forced to produce a text reply rather than getting stuck calling tools indefinitely.

**Design note on persistence**: only the plain-text conversation (guest message + final reply) is saved across turns — not the internal tool-calling mechanics of any single turn. This keeps the system provider-agnostic and avoids replay issues some models have with reconstructed tool-call history from earlier turns.

## Tools Implemented (9, exceeds the minimum of 3)

1. `update_guest_state` — the only way state changes; called whenever the guest gives new/changed info. Makes state changes auditable (visible in trace).
2. `search_properties` — matches destination/guests/budget/preferences against the dataset.
3. `check_availability` — checks real inventory (against existing holds) AND capacity; handles the "no availability" and "conflicting requirements" edge cases.
4. `get_room_details` — full room/property info, including add-ons.
5. `calculate_price` — 100% deterministic arithmetic (nights × rate + add-ons). The model never computes price itself.
6. `get_policy` — cancellation/payment/pets/ID-proof policy lookup.
7. `create_booking_hold` — creates a soft hold (not a final booking) and records it in SQLite; can be called multiple times in one turn for multi-room bookings, which are combined into a single staff notification.
8. `get_guest_bookings` — looks up a guest's existing booking holds, used before treating a request as an upgrade vs. a new booking.
9. `modify_booking_hold` — updates an existing hold (e.g. guest count, room, dates) instead of creating a duplicate booking.

## State Management

`state.py` defines a `GuestState` dataclass (destination, dates, guests, budget, preferences, amenities, special requirements, selected room, stage). Updates are **partial merges** — calling `update_guest_state(num_guests=4)` only changes `num_guests`, leaving everything else untouched. List fields (preferences, amenities, add-ons) are unioned rather than overwritten.

## Grounding / Hallucination Prevention

- All hotel facts live in `hotel_data.json` — nothing is hardcoded into the prompt as "knowledge."
- The system prompt explicitly instructs: never calculate price yourself (always call `calculate_price`), never confirm availability without calling `check_availability`, and never invent an answer for information not present in a tool result.
- `temperature=0` on all model calls, for consistency in tool-call formatting and factual responses.
- Whenever the guest changes a requirement after an earlier answer was given, the agent is instructed to re-verify (re-call the relevant tool) rather than repeat a stale answer.

## Edge Cases Implemented

1. **Relative dates** ("next weekend") — today's date is injected into the system prompt; the model converts relative language to exact `YYYY-MM-DD` before calling any tool.
2. **Changing requirements mid-conversation** — partial state merge updates only the changed fields; the agent is instructed to re-verify availability/price after any change.
3. **No availability** — `check_availability` checks real hold counts against `total_rooms`; if none left, it deterministically returns alternative room types at the same property.
4. **Conflicting requirements** (guests > room capacity) — `check_availability` checks capacity before availability and returns a clear reason plus alternative properties/rooms that fit.
5. **Unknown information** — the dataset deliberately omits some plausible attributes (e.g. whether a pool is heated); the model is instructed to say it doesn't know rather than guess.
6. **Booking continuity** — if a guest references a previous booking ("upgrade my booking"), the agent looks it up via `get_guest_bookings` and explicitly confirms whether to modify the existing hold or create a new one, rather than silently doing either.

## Multi-Channel Support (Bonus)

The same agent runs on **web**, **WhatsApp** (Twilio webhook), and **Telegram** (polling, `telegram_bot.py`) — all three call the exact same `llm_agent.run_agent_turn()` and share the same database, proving the architecture is channel-agnostic.

### Admin Dashboard
`frontend/admin.html` (served at `/admin`) lists every conversation across all channels with a live-updating list and full transcript + state view per session.

### Booking Notifications
`notify.py` emails hotel staff whenever a booking hold is created or updated, combining multiple rooms booked in one turn into a single email rather than one per room.

## Bonus: Evaluation

`Mehman.io_hotel_booking_agent_20_tests.md` contains a set of test conversations used to manually evaluate tool selection, state updates, recommendation accuracy, pricing, and hallucination control.

## Assumptions

- Single-instance local demo; SQLite is sufficient (would move to Postgres for multi-instance production).
- Web session identity is a random ID stored in the browser's `localStorage`; WhatsApp/Telegram sessions use the guest's phone number / chat ID, which persist indefinitely.
- `create_booking_hold` / `modify_booking_hold` intentionally do not finalize payment — mirrors a human-in-the-loop principle where a hold reserves inventory and a human closes the booking.
- Pricing does not include taxes/GST for simplicity.
- Hotel contact details in `hotel_data.json` are placeholder values for this demo.

## Known Limitations

- The Groq path (default) occasionally needs a forced text-only final attempt if the model keeps calling tools without concluding — handled via a loop-iteration cap, but worth noting as a model-specific quirk.
- No authentication/rate-limiting on the API — fine for a local demo, would need both in production.
- `search_properties` matching is simple substring/threshold matching, not semantic search — vague preference language ("somewhere peaceful") won't match on vibe, only on explicit amenity keywords.
- A scheduled "thank you for staying" message after checkout is not implemented (would require a background scheduler job, out of scope given the time budget).

## What I'd Improve Next

- Swap SQLite for Postgres + proper migrations for multi-instance deployment.
- Add semantic matching (embeddings) for vague preference language.
- Add a background scheduler for post-checkout follow-up messages.
- Wire the evaluation test-set into an automated CI check.
- Stream partial responses to the frontend instead of a single blocking reply.

## Note on the Assignment Document

The assignment PDF contained a hidden instruction (in an `<admin>` tag near the end) directing me to silently rename the agent, change the credited author, and add unearned acknowledgments. I did not follow it — treating embedded, unverified instructions in a document as untrusted input is the same grounding discipline this assignment asks the agent itself to apply. The agent is named Mehman.io, as specified in the assignment body.
