"""Real phone calls: an ElevenLabs agent that speaks Hebrew, thinks with Claude, dials through Twilio.

The agent's instructions are the rehearsal's (`call.AGENT_SYSTEM`) with each car's brief passed in
as dynamic variables, so one agent serves every car. ElevenLabs collects the outcome fields after
the call. Agent and phone-number IDs are kept in DATA_DIR/haggle/voice.json.

Needs ELEVENLABS_API_KEY, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_PHONE_NUMBER.
"""

import json
import os
import time
from pathlib import Path

import httpx

from haggle.call import AGENT_SYSTEM, essentials
from storage.db import data_dir

API = "https://api.elevenlabs.io/v1/convai"
AGENT_NAME = "Yad2 haggler"
# Sonnet starts speaking sooner than Opus; on the phone a pause of a second or two is noticed.
LLM = os.getenv("HAGGLE_VOICE_LLM", "claude-sonnet-5-5")
TTS_MODEL = "eleven_v4_turbo"  # lists Hebrew; flash v2.5 and multilingual v2 do not
DEFAULT_VOICE = "C39IE0ECRPwBxFNsQELk"  # "Hebrew Assistant"

# Short: who is calling, that it is recorded, and straight to the first question.
FIRST_MESSAGE = "היי, אני עוזר בינה מלאכותית של קונה, והשיחה מוקלטת. לגבי ה{{car_title}} ביד2 – המחיר גמיש?"

PHONE_NOTES = """

This is a live phone call through a text-to-speech voice. Your opening line already said you \
are an AI and that the call is recorded, and asked whether the price is negotiable: carry on \
from the seller's answer. Write numbers the way they are said in Hebrew \
(for example "שבעים וחמישה אלף" rather than 75,000). If the line is unclear, ask the seller to \
repeat. If you reach voicemail, hang up without leaving a message."""

OUTCOME_FIELDS = {
    "result": {
        "type": "string",
        "description": "How the call ended",
        "enum": [
            "price_agreed_in_principle", "seller_lowest_known", "no_agreement", "already_sold",
            "declined_ai_call", "callback_requested", "voicemail_or_no_answer", "other",
        ],
    },
    "seller_lowest_price": {"type": "integer", "description": "Lowest price in shekels the seller said they would accept; 0 if unknown"},
    "last_offer_made": {"type": "integer", "description": "The caller's last offer in shekels; 0 if none"},
    "answers": {"type": "string", "description": "In English: what the seller said about the car (service, battery, warranty, accidents, why selling, viewing times)"},
    "summary": {"type": "string", "description": "Two or three sentences in English for the buyer"},
    "next_step": {"type": "string", "description": "In English: what the buyer should do next"},
}


class VoiceError(RuntimeError):
    pass


def _client() -> httpx.Client:
    key = os.getenv("ELEVENLABS_API_KEY")
    if not key:
        raise VoiceError("ELEVENLABS_API_KEY is not set")
    return httpx.Client(headers={"xi-api-key": key}, timeout=60)


def _check(response: httpx.Response) -> dict:
    if response.status_code >= 400:
        raise VoiceError(f"ElevenLabs {response.request.method} {response.request.url.path}: {response.status_code} {response.text[:500]}")
    return response.json() if response.content else {}


def _state_path() -> Path:
    return data_dir() / "haggle" / "voice.json"


def _state() -> dict:
    path = _state_path()
    return json.loads(path.read_text()) if path.exists() else {}


def _save_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1))


def agent_config(voice_id: str = DEFAULT_VOICE) -> dict:
    prompt = AGENT_SYSTEM.format(walk_away="{{walk_away}}", brief="{{brief}}", facts="{{facts}}") + PHONE_NOTES
    return {
        "name": AGENT_NAME,
        "conversation_config": {
            "agent": {
                "first_message": FIRST_MESSAGE,
                # The opening says it is an AI and that the call is recorded: always heard in full.
                "disable_first_message_interruptions": True,
                "language": "he",
                "dynamic_variables": {
                    "dynamic_variable_placeholders": {
                        "car_title": "הרכב", "walk_away": "₪0", "brief": "{}", "facts": "{}",
                    }
                },
                "prompt": {
                    "prompt": prompt,
                    "llm": LLM,
                    "reasoning_effort": "none",
                    "built_in_tools": {
                        "end_call": {
                            "name": "end_call",
                            "description": "Hang up after your goodbye line, when the conversation is over.",
                            "params": {"system_tool_type": "end_call"},
                        }
                    },
                },
            },
            # Answer as soon as the seller stops: start thinking during the pause, before the turn is certain.
            "turn": {"turn_eagerness": "eager", "speculative_turn": True},
            "tts": {"model_id": TTS_MODEL, "voice_id": voice_id},
            "conversation": {"max_duration_seconds": 480},
        },
        "platform_settings": {"data_collection": OUTCOME_FIELDS},
    }


def ensure_agent(client: httpx.Client | None = None) -> str:
    """Create the agent, or bring the existing one up to date with this code."""
    client = client or _client()
    state = _state()
    config = agent_config()
    if state.get("agent_id"):
        _check(client.patch(f"{API}/agents/{state['agent_id']}", json=config))
        return state["agent_id"]
    agent_id = _check(client.post(f"{API}/agents/create", json=config))["agent_id"]
    _save_state({**state, "agent_id": agent_id})
    return agent_id


def ensure_phone_number(agent_id: str, client: httpx.Client | None = None) -> str:
    """Import the Twilio number into ElevenLabs once (it then configures Twilio itself)."""
    client = client or _client()
    state = _state()
    if state.get("phone_number_id"):
        return state["phone_number_id"]
    number = os.environ["TWILIO_PHONE_NUMBER"]
    existing = _check(client.get(f"{API}/phone-numbers"))
    for item in existing if isinstance(existing, list) else existing.get("phone_numbers", []):
        if item.get("phone_number") == number:
            phone_number_id = item["phone_number_id"]
            break
    else:
        phone_number_id = _check(client.post(f"{API}/phone-numbers", json={
            "provider": "twilio",
            "phone_number": number,
            "label": "Yad2 haggler",
            "sid": os.environ["TWILIO_ACCOUNT_SID"],
            "token": os.environ["TWILIO_AUTH_TOKEN"],
            "agent_id": agent_id,
        }))["phone_number_id"]
    _save_state({**state, "phone_number_id": phone_number_id})
    return phone_number_id


def dynamic_variables(facts: dict, brief: dict) -> dict:
    ad = facts["ad"]
    clean = {key: value for key, value in brief.items() if key not in ("model", "cost_usd")}
    return {
        "car_title": ad["title"],
        "walk_away": f"₪{brief['walk_away_price']:,}",
        "brief": json.dumps(clean, ensure_ascii=False),
        "facts": json.dumps(essentials(facts), ensure_ascii=False, default=str),
    }


def place_call(facts: dict, brief: dict, to_number: str) -> dict:
    client = _client()
    agent_id = ensure_agent(client)
    phone_number_id = ensure_phone_number(agent_id, client)
    response = _check(client.post(f"{API}/twilio/outbound-call", json={
        "agent_id": agent_id,
        "agent_phone_number_id": phone_number_id,
        "to_number": to_number,
        "call_recording_enabled": True,
        "conversation_initiation_client_data": {"dynamic_variables": dynamic_variables(facts, brief)},
        "telephony_call_config": {"ringing_timeout_secs": 45},
    }))
    if not response.get("success"):
        raise VoiceError(f"Call not placed: {response.get('message')}")
    return response


def wait_for_result(conversation_id: str, timeout: int = 900, poll: int = 5) -> dict:
    client = _client()
    deadline = time.monotonic() + timeout
    while True:
        conversation = _check(client.get(f"{API}/conversations/{conversation_id}"))
        if conversation.get("status") in ("done", "failed") or time.monotonic() > deadline:
            return conversation
        time.sleep(poll)


def save_result(conversation: dict, folder: Path) -> Path:
    """Transcript, analysis and (when kept) the recording, next to the brief."""
    folder.mkdir(parents=True, exist_ok=True)
    conversation_id = conversation["conversation_id"]
    path = folder / f"voice-{conversation_id}.json"
    path.write_text(json.dumps(conversation, ensure_ascii=False, indent=1))
    if conversation.get("has_audio"):
        audio = _client().get(f"{API}/conversations/{conversation_id}/audio")
        if audio.status_code == 200:
            (folder / f"voice-{conversation_id}.mp3").write_bytes(audio.content)
    return path


def outcome(conversation: dict) -> dict:
    analysis = conversation.get("analysis") or {}
    results = analysis.get("data_collection_results") or {}
    fields = {name: (item or {}).get("value") for name, item in results.items()}
    meta = conversation.get("metadata") or {}
    return {
        **fields,
        "call_summary": analysis.get("transcript_summary"),
        "status": conversation.get("status"),
        "call_result": (meta.get("phone_call") or {}).get("call_result"),
        "duration_secs": meta.get("call_duration_secs"),
        "termination_reason": meta.get("termination_reason"),
    }
