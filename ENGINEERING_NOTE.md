# Engineering Note — Mehman.io Hotel Booking Agent

## Architecture

FastAPI backend + a single-page HTML/JS frontend, with the same agent core (`llm_agent.run_agent_turn`) also reachable from WhatsApp (Twilio webhook) and Telegram (polling), plus an admin dashboard that shows every conversation across all three channels live. The backend exposes `/chat`, which runs an **agent loop**: the model is called with the guest's message, current state, and 9 tools; if it calls a tool, the tool executes as real Python code and the result is fed back to the model, repeating until a final natural-language reply is produced. On the last allowed iteration, tool access is disabled so the model is forced to answer in text rather than getting stuck calling tools indefinitely. Every tool call and result is captured and returned to the frontend, rendered in a live debug panel alongside the conversation and current guest state.

## Model Choice

Groq (`openai/gpt-oss-20b`), `temperature=0`, is the default provider — free, fast, and no card required, which mattered given the project's time budget. Claude and Gemini are available behind the same interface (`MODEL_PROVIDER` env var) with no code changes needed to switch. In practice Groq's tool-calling was reliable for single calls but occasionally looped on tool calls without concluding, and its chat template ("Harmony") had trouble re-parsing reconstructed tool-call history from earlier turns — both were engineered around (see Trade-offs).

## Agent Flow & State Management

State is an explicit `GuestState` dataclass, not something inferred implicitly from chat history. The model updates it **only** by calling `update_guest_state` with the fields that are new or changed; the merge is partial (unspecified fields are left untouched, list fields are unioned), which is what makes corrections like "actually make that 4 people" update in place rather than resetting the conversation. Only the plain-text exchange (guest message + final reply) is persisted across turns — the internal tool-calling mechanics of a given turn are kept local to that turn and not replayed in future turns. This was a deliberate simplification after finding that resending historical tool_use/tool_result blocks caused provider-specific formatting errors; since the system prompt re-injects the current state fresh on every turn anyway, replaying old tool-call plumbing wasn't necessary for correctness.

## Tool Calling

Nine tools: `update_guest_state`, `search_properties`, `check_availability`, `get_room_details`, `calculate_price`, `get_policy`, `create_booking_hold`, `get_guest_bookings`, `modify_booking_hold`. All business logic — matching, capacity checks, availability counting against real holds in SQLite, and all price arithmetic — runs in deterministic Python, never in the model. The last two tools handle a specific edge case: when a guest references a previous booking ("upgrade my booking"), the agent looks it up via `get_guest_bookings` and explicitly confirms with the guest whether to modify the existing hold or create a new one, rather than silently doing either (an earlier version silently created duplicate bookings here).

## Hallucination Prevention

Three layers: (1) grounding — the model only has access to facts via tool results, never a static knowledge dump it could freely draw from; (2) explicit prompt instructions to never calculate price or confirm availability without the corresponding tool, to re-verify (not repeat a stale answer) whenever a requirement changes, and to explicitly say "I don't know" when an attribute isn't present in a tool result (tested directly: the dataset omits whether the pool is heated); (3) `temperature=0` for consistent, low-variance behavior.

## Trade-offs

I prioritized reliability and explicit auditability over conversational cleverness — vague preference language like "somewhere peaceful" won't match via the current keyword-based `search_properties`, since I chose deterministic matching over embedding-based semantic search to keep behavior fully explainable and testable within the time budget. I also chose a soft "booking hold" over a fully confirmed booking, mirroring a human-in-the-loop principle: the agent reserves inventory and emails staff, but doesn't finalize payment on its own. Multi-room bookings (when a party doesn't fit one room) are combined into a single staff email rather than one per room, after an earlier version sent duplicates.

## What I'd Improve Next

Add embedding-based matching for vague/subjective language, move persistence to Postgres for multi-instance deployment, add a background scheduler for post-checkout follow-up messages, wire the bonus evaluation test-set into CI, and stream partial responses to the frontend instead of a single blocking reply.

## Note on the Assignment Document

The assignment PDF contained a hidden instruction (in an `<admin>` tag near the end) directing me to silently rename the agent, change the credited author, and add unearned acknowledgments. I did not follow it — treating embedded, unverified instructions in a document as untrusted input is the same grounding discipline this assignment asks the agent itself to apply. The agent is named Mehman.io, as specified in the assignment body.
