"""
llm_agent.py
------------
This is the agent's "brain": the system prompt, the tool schemas, and the
AGENT LOOP that lets the model call multiple tools in sequence before
replying (e.g. update_guest_state -> search_properties -> check_availability
-> final natural-language response), which is what the assignment calls
"decide what is known, what is missing, whether to ask a question or use
a tool, and what should happen next."
 
Supports Claude (primary -- best tool-calling reliability) and Groq
(fallback/free option) behind one interface, same pattern as the earlier
WhatsApp project.
"""
 
import os
import json
import time
from datetime import datetime
from typing import List, Dict, Any
from dotenv import load_dotenv
 
load_dotenv()  # safeguard -- ensures MODEL_PROVIDER/API keys are available
                # even if this module gets imported before main.py's own
                # load_dotenv() call (load_dotenv() is safe to call multiple times).
 
import tools
from state import GuestState
 
MODEL_PROVIDER = os.getenv("MODEL_PROVIDER", "groq")  # groq is default: free, fast, no rate-limit
                                                        # issues like Gemini's free tier had for us
MAX_TOOL_ITERATIONS = 4
 
# ---------------------------------------------------------------------------
# Tool schemas -- session_id is deliberately NOT exposed to the model; it's
# injected server-side when we execute the call. This stops the model from
# ever having to "know" or invent a session id.
# ---------------------------------------------------------------------------
TOOL_DEFS = [
    {
        "name": "update_guest_state",
        "description": (
            "Call this FIRST whenever the guest gives new or changed information "
            "(destination, dates, number of guests, budget, room preferences, "
            "amenities wanted, special requirements). Only pass the fields that "
            "are new or have changed -- do not repeat unchanged fields. This is "
            "the ONLY way guest information gets remembered."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "destination": {"type": "string"},
                "check_in": {"type": "string", "description": "YYYY-MM-DD"},
                "check_out": {"type": "string", "description": "YYYY-MM-DD"},
                "num_guests": {"type": "integer"},
                "budget_per_night_inr": {"type": "integer"},
                "room_preferences": {"type": "array", "items": {"type": "string"}},
                "amenities_wanted": {"type": "array", "items": {"type": "string"}},
                "special_requirements": {"type": "string"},
            },
        },
    },
    {
        "name": "search_properties",
        "description": "Search for properties/rooms matching the guest's known criteria. Use after enough state is known (at least destination or guests or budget).",
        "input_schema": {
            "type": "object",
            "properties": {
                "destination": {"type": "string"},
                "num_guests": {"type": "integer"},
                "budget_per_night_inr": {"type": "integer"},
                "room_preferences": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "check_availability",
        "description": "Check if a specific room type at a specific property is available for exact dates. ALWAYS call this before confirming a room is bookable.",
        "input_schema": {
            "type": "object",
            "properties": {
                "property_id": {"type": "string"},
                "room_type": {"type": "string"},
                "check_in": {"type": "string", "description": "YYYY-MM-DD"},
                "check_out": {"type": "string", "description": "YYYY-MM-DD"},
                "num_guests": {"type": "integer"},
            },
            "required": ["property_id", "room_type", "check_in", "check_out"],
        },
    },
    {
        "name": "get_room_details",
        "description": "Get full details (amenities, description, add-ons) for a specific room at a property. Use when the guest asks about specifics of a room/property.",
        "input_schema": {
            "type": "object",
            "properties": {
                "property_id": {"type": "string"},
                "room_type": {"type": "string"},
            },
            "required": ["property_id", "room_type"],
        },
    },
    {
        "name": "calculate_price",
        "description": "Calculate the exact total price for a stay, including any add-ons. NEVER calculate or estimate price yourself in text -- always call this tool.",
        "input_schema": {
            "type": "object",
            "properties": {
                "property_id": {"type": "string"},
                "room_type": {"type": "string"},
                "check_in": {"type": "string"},
                "check_out": {"type": "string"},
                "add_ons": {"type": "array", "items": {"type": "string"}},
                "num_guests": {"type": "integer"},
            },
            "required": ["property_id", "room_type", "check_in", "check_out"],
        },
    },
    {
        "name": "get_policy",
        "description": "Get cancellation, payment, pet, or ID-proof policy for a property.",
        "input_schema": {
            "type": "object",
            "properties": {
                "property_id": {"type": "string"},
                "policy_type": {"type": "string", "description": "e.g. cancellation, payment, pets, id_proof. Omit for all policies."},
            },
            "required": ["property_id"],
        },
    },
    {
        "name": "create_booking_hold",
        "description": "Create a temporary booking hold once the guest has confirmed they want to proceed with a specific room and has given their name and guest count. This does NOT finalize payment -- it reserves inventory and notifies staff.",
        "input_schema": {
            "type": "object",
            "properties": {
                "property_id": {"type": "string"},
                "room_type": {"type": "string"},
                "check_in": {"type": "string"},
                "check_out": {"type": "string"},
                "guest_name": {"type": "string"},
                "num_guests": {"type": "integer"},
                "phone_number": {"type": "string"},
                "add_ons": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["property_id", "room_type", "check_in", "check_out", "guest_name", "num_guests"],
        },
    },
]
 
TOOL_FUNCTIONS = {
    "update_guest_state": tools.update_guest_state,
    "search_properties": tools.search_properties,
    "check_availability": tools.check_availability,
    "get_room_details": tools.get_room_details,
    "calculate_price": tools.calculate_price,
    "get_policy": tools.get_policy,
    "create_booking_hold": tools.create_booking_hold,
}
 
 
def _dispatch_provider_call(history: list, system_prompt: str) -> dict:
    if MODEL_PROVIDER == "groq":
        return _call_groq(history, system_prompt)
    elif MODEL_PROVIDER == "gemini":
        return _call_gemini(history, system_prompt)
    return _call_claude(history, system_prompt)
 
 
def _call_provider_with_retry(history: list, system_prompt: str, max_retries: int = 2) -> dict:
    """Free-tier APIs (Gemini, Groq) can hit rate limits (HTTP 429) under
    normal use, especially with our multi-step tool loop firing several
    calls per guest message. Retries with a short backoff before giving
    up, instead of surfacing a transient rate-limit as a hard failure."""
    result = {}
    for attempt in range(max_retries + 1):
        result = _dispatch_provider_call(history, system_prompt)
        error = result.get("error", "")
        is_rate_limit = "429" in error or "rate limit" in error.lower() or "too many requests" in error.lower()
        if is_rate_limit and attempt < max_retries:
            wait_seconds = 3 * (attempt + 1)  # 3s, then 6s
            print(f"[llm_agent.py] Rate limited, retrying in {wait_seconds}s (attempt {attempt + 1}/{max_retries})...")
            time.sleep(wait_seconds)
            continue
        return result
    return result
 
 
def build_system_prompt(state: GuestState) -> str:
    today = datetime.now().strftime("%Y-%m-%d (%A)")
    return f"""You are Mira, a guest-facing hotel booking assistant for a hospitality platform \
covering multiple properties across India.
 
Today's date is: {today}
 
CURRENT KNOWN GUEST STATE (for your reference -- keep this updated via update_guest_state):
{json.dumps(state.to_dict(), indent=2)}
 
YOUR JOB:
- Understand the guest's request from natural language, however they phrase it.
- Whenever the guest gives new or CHANGED information, call update_guest_state FIRST,
  before anything else, with only the fields that are new/changed.
- When the guest uses a relative date ("next weekend", "in 3 days"), convert it to an
  exact YYYY-MM-DD date yourself using today's date above, then pass the exact date to
  update_guest_state and any other tool. Never pass relative date words into a tool.
- Decide what is still missing, and either ask ONE clear question, or call the
  appropriate tool if you have enough information.
- EFFICIENCY: if you already know enough to call more than one tool (e.g. you
  just learned new state AND already have enough to search or check
  availability), call them together in the SAME turn rather than spreading
  them across multiple back-and-forth turns -- this keeps replies fast.
- Use search_properties once you know at least the destination or guest count or budget.
- ALWAYS call check_availability before telling the guest a room is available.
- NEVER calculate prices yourself -- always call calculate_price and report its result exactly.
- If a tool result says something is unavailable, or capacity is exceeded, explain this to
  the guest and offer the suggested alternatives from the tool result.
- If the guest asks about an attribute that is not present in a tool's result or the hotel
  data (e.g. "is the pool heated?"), say clearly that you don't have that information --
  NEVER guess or invent an answer. Offer to find out or suggest they ask on-site.
- Keep the conversation natural and conversational -- do not read out raw JSON or make it
  feel like a form. Move the conversation toward a booking, one step at a time.
- Only call create_booking_hold after the guest has clearly confirmed they want to proceed
  with a specific room, and you have their name and guest count.
- Never guarantee a booking is 100% final -- create_booking_hold only creates a hold;
  a human team member follows up to confirm payment.
- CRITICAL: whenever the guest changes any requirement (dates, guests, room, add-ons) AFTER
  you already gave a recommendation or price, you MUST call check_availability and/or
  calculate_price AGAIN with the new details before replying. NEVER reuse an earlier tool
  result or repeat an earlier price/availability answer after something has changed --
  always re-verify against the current state.
- If the guest asks something like "do you remember me?" or references earlier context,
  answer truthfully based on the actual current state shown above -- do not just say "yes"
  without checking it's actually still accurate.
"""
 
 
def _execute_tool(session_id: str, name: str, tool_input: dict) -> dict:
    fn = TOOL_FUNCTIONS.get(name)
    if not fn:
        return {"error": f"Unknown tool: {name}"}
    # Inject session_id server-side for the tools that need it
    if name in ("update_guest_state", "create_booking_hold"):
        tool_input = {**tool_input, "session_id": session_id}
    try:
        return fn(**tool_input)
    except Exception as e:
        return {"error": f"Tool execution failed: {e}"}
 
 
def run_agent_turn(session_id: str, user_message: str) -> Dict[str, Any]:
    """Runs one full agent turn: may involve several tool calls before the
    final natural-language reply. Returns the reply plus a full trace of
    every tool call + result, for the UI's debug panel."""
    import db
    history = db.load_history(session_id)
    history.append({"role": "user", "content": user_message})
 
    trace = []
    final_text = ""
 
    for _ in range(MAX_TOOL_ITERATIONS):
        state = db.load_state(session_id)
        system_prompt = build_system_prompt(state)
 
        result = _call_provider_with_retry(history, system_prompt)
 
        if result.get("error"):
            trace.append({"type": "error", "message": result["error"]})
            final_text = "Sorry, I ran into an issue processing that. Could you try rephrasing?"
            break
 
        # Append assistant's turn (text + any tool_use) to history in provider-neutral form
        history.append({"role": "assistant", "content": result["raw_content"]})
 
        if not result["tool_calls"]:
            final_text = result["text"]
            break
 
        tool_results_for_history = []
        for call in result["tool_calls"]:
            tool_output = _execute_tool(session_id, call["name"], call["input"])
            trace.append({"tool": call["name"], "input": call["input"], "result": tool_output})
            tool_results_for_history.append({
                "tool_use_id": call["id"],
                "name": call["name"],
                "output": tool_output,
            })
 
        history.append({"role": "user", "content": _format_tool_results(tool_results_for_history)})
    else:
        # Loop exhausted MAX_TOOL_ITERATIONS without a final text-only reply
        final_text = final_text or "Sorry, that's taking a bit long -- could you rephrase or simplify your request?"
 
    # Make sure the final reply is always represented in saved history exactly
    # once. If the last saved turn was already this exact assistant text
    # (the normal happy-path case, via raw_content above), don't duplicate it.
    last_msg = history[-1] if history else None
    already_saved = (
        last_msg
        and last_msg["role"] == "assistant"
        and isinstance(last_msg["content"], list)
        and any(b.get("type") == "text" and b.get("text", "").strip() == final_text.strip() for b in last_msg["content"])
    )
    if not already_saved:
        history.append({"role": "assistant", "content": final_text})
 
    db.save_history(session_id, history)
 
    final_state = db.load_state(session_id)
    return {"reply": final_text, "trace": trace, "state": final_state.to_dict()}
 
 
def _format_tool_results(tool_results: list):
    """Claude expects tool_result content blocks; we store them in a
    provider-neutral intermediate form and reformat per-provider inside the
    _call_* functions when needed. For simplicity here we standardize on
    Claude's format since Claude is the primary provider."""
    return [
        {
            "type": "tool_result",
            "tool_use_id": tr["tool_use_id"],
            "name": tr["name"],
            "content": json.dumps(tr["output"]),
        }
        for tr in tool_results
    ]
 
 
def _call_claude(history: list, system_prompt: str) -> dict:
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
 
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            temperature=0,
            system=system_prompt,
            tools=TOOL_DEFS,
            messages=history,
        )
    except Exception as e:
        return {"error": str(e)}
 
    text_parts, tool_calls, raw_content = [], [], []
    for block in resp.content:
        if block.type == "text":
            text_parts.append(block.text)
            raw_content.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            tool_calls.append({"id": block.id, "name": block.name, "input": block.input})
            raw_content.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
 
    return {"text": " ".join(text_parts).strip(), "tool_calls": tool_calls, "raw_content": raw_content}
 
 
def _call_groq(history: list, system_prompt: str) -> dict:
    """Groq/OpenAI-compatible fallback. Note: tool-result formatting differs
    from Claude's, so history format is translated here for this provider."""
    from openai import OpenAI
    client = OpenAI(api_key=os.environ["GROQ_API_KEY"], base_url="https://api.groq.com/openai/v1")
 
    openai_tools = [{"type": "function", "function": {
        "name": t["name"], "description": t["description"], "parameters": t["input_schema"]
    }} for t in TOOL_DEFS]
 
    # Translate our Claude-style history into OpenAI-style messages
    oa_messages = [{"role": "system", "content": system_prompt}]
    for msg in history:
        if isinstance(msg["content"], str):
            oa_messages.append({"role": msg["role"], "content": msg["content"]})
        elif isinstance(msg["content"], list):
            for block in msg["content"]:
                if block.get("type") == "text":
                    oa_messages.append({"role": msg["role"], "content": block["text"]})
                elif block.get("type") == "tool_result":
                    oa_messages.append({
                        "role": "tool",
                        "tool_call_id": block["tool_use_id"],
                        "name": block.get("name", "unknown_tool"),
                        "content": block["content"],
                    })
                elif block.get("type") == "tool_use":
                    oa_messages.append({"role": "assistant", "content": None, "tool_calls": [{
                        "id": block["id"], "type": "function",
                        "function": {"name": block["name"], "arguments": json.dumps(block["input"])}
                    }]})
 
    try:
        resp = client.chat.completions.create(
            model="openai/gpt-oss-20b", max_tokens=800, temperature=0,
            messages=oa_messages, tools=openai_tools,
        )
    except Exception as e:
        return {"error": str(e)}
 
    choice = resp.choices[0].message
    tool_calls, raw_content = [], []
    if choice.tool_calls:
        for tc in choice.tool_calls:
            tool_input = json.loads(tc.function.arguments)
            tool_calls.append({"id": tc.id, "name": tc.function.name, "input": tool_input})
            raw_content.append({"type": "tool_use", "id": tc.id, "name": tc.function.name, "input": tool_input})
    text = choice.content or ""
    if text:
        raw_content.append({"type": "text", "text": text})
 
    return {"text": text.strip(), "tool_calls": tool_calls, "raw_content": raw_content}
 
 
def _call_gemini(history: list, system_prompt: str) -> dict:
    """Gemini via plain REST (no extra SDK dependency needed -- we already
    use `requests` elsewhere in the project). Gemini's function-calling
    format differs from both Claude and OpenAI: tool calls don't carry an
    id, so we generate a synthetic one to keep our generic tool-execution
    loop working, and tool RESULTS need the tool's `name` (not an id), so
    we rebuild a tool_use_id -> name map from history on every call."""
    import requests as req
 
    model = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
    api_key = os.environ["GEMINI_API_KEY"]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
 
    gemini_tools = [{"functionDeclarations": [
        {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}
        for t in TOOL_DEFS
    ]}]
 
    id_to_name = {}
    for msg in history:
        if isinstance(msg["content"], list):
            for block in msg["content"]:
                if block.get("type") == "tool_use":
                    id_to_name[block["id"]] = block["name"]
 
    contents = []
    for msg in history:
        role = "model" if msg["role"] == "assistant" else "user"
        if isinstance(msg["content"], str):
            contents.append({"role": role, "parts": [{"text": msg["content"]}]})
        elif isinstance(msg["content"], list):
            parts = []
            for block in msg["content"]:
                if block.get("type") == "text":
                    parts.append({"text": block["text"]})
                elif block.get("type") == "tool_use":
                    parts.append({"functionCall": {"name": block["name"], "args": block["input"]}})
                elif block.get("type") == "tool_result":
                    name = id_to_name.get(block["tool_use_id"], "unknown_tool")
                    try:
                        response_obj = json.loads(block["content"])
                    except Exception:
                        response_obj = {"result": block["content"]}
                    parts.append({"functionResponse": {"name": name, "response": response_obj}})
            if parts:
                contents.append({"role": role, "parts": parts})
 
    body = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "tools": gemini_tools,
        "generationConfig": {"temperature": 0, "maxOutputTokens": 800},
    }
 
    try:
        resp = req.post(url, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return {"error": str(e)}
 
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError):
        return {"error": f"Unexpected Gemini response: {data}"}
 
    text_parts, tool_calls, raw_content = [], [], []
    for i, part in enumerate(parts):
        if "text" in part:
            text_parts.append(part["text"])
            raw_content.append({"type": "text", "text": part["text"]})
        elif "functionCall" in part:
            fc = part["functionCall"]
            call_id = f"gemini_call_{i}_{fc['name']}"
            args = fc.get("args", {})
            tool_calls.append({"id": call_id, "name": fc["name"], "input": args})
            raw_content.append({"type": "tool_use", "id": call_id, "name": fc["name"], "input": args})
 
    return {"text": " ".join(text_parts).strip(), "tool_calls": tool_calls, "raw_content": raw_content}