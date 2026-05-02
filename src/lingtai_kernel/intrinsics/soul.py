"""Soul intrinsic — the agent's inner voice.

Three actions:
    flow    — past-self consultation appendix. Every ``_soul_delay`` seconds,
              fires M=1+K parallel LLM calls (1 stepped-back read of the
              current chat as "insights", K random past-snapshot
              consultations sampled from history/snapshots/). Voices bundle
              into one synthetic (assistant{tool_call}, user{tool_result})
              pair with action="flow"; the pair is enqueued on tc_inbox
              with replace_in_history=True so the drain side enforces a
              single-slot invariant in chat history. Mechanical — agent
              cannot invoke manually.
    inquiry — sync mirror session. Clones conversation (text+thinking only),
              sends question, returns answer in tool result. On-demand.
    config  — adjust soul flow knobs. Accepts any subset of three optional
              fields: delay_seconds (wall-clock cadence), consultation_interval
              (turn-counter cadence), consultation_past_count (K, number of
              past-self voices per fire). Updates live state, restarts the
              wall-clock timer if delay changed, persists to init.json.
"""
from __future__ import annotations


# Lower bound on agent-set soul delay. Below this, the consultation cost
# (M parallel LLM calls per fire) dominates the agent's own turns.
SOUL_DELAY_MIN_SECONDS = 30.0
# Lower bound on turn-counter cadence — below this, every few turns triggers
# a fire and consultation cost dominates work. 0 disables the turn counter.
CONSULTATION_INTERVAL_MIN = 5
# Bounds on K — past-self voice count per fire. 0 = insights-only fires;
# 5 caps M=6 LLM calls per fire (cost + chat-history bloat).
CONSULTATION_PAST_COUNT_MIN = 0
CONSULTATION_PAST_COUNT_MAX = 5

# Built-in voice profile names. The agent can switch between these or set
# a custom prompt via soul(action='voice'). Order here = order shown in
# the read response.
SOUL_VOICE_BUILTINS = ("inner", "observer")
# Cap on agent-supplied custom voice prompts. Comfortable budget — the
# observer preset is ~580 chars; 4000 is generous without inviting
# system-prompt-stuffing as a side channel.
SOUL_VOICE_PROMPT_MAX = 4000


def get_description(lang: str = "en") -> str:
    from ..i18n import t
    return t(lang, "soul.description")


def get_schema(lang: str = "en") -> dict:
    from ..i18n import t
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["inquiry", "flow", "config", "voice"],
                "description": t(lang, "soul.action_description"),
            },
            "inquiry": {
                "type": "string",
                "description": t(lang, "soul.inquiry_description"),
            },
            "delay_seconds": {
                "type": "number",
                "minimum": SOUL_DELAY_MIN_SECONDS,
                "description": t(lang, "soul.delay_seconds_description"),
            },
            "consultation_interval": {
                "type": "integer",
                "minimum": 0,
                "description": t(lang, "soul.consultation_interval_description"),
            },
            "consultation_past_count": {
                "type": "integer",
                "minimum": CONSULTATION_PAST_COUNT_MIN,
                "maximum": CONSULTATION_PAST_COUNT_MAX,
                "description": t(lang, "soul.consultation_past_count_description"),
            },
            "set": {
                "type": "string",
                "description": t(lang, "soul.voice_set_description"),
            },
            "prompt": {
                "type": "string",
                "maxLength": SOUL_VOICE_PROMPT_MAX,
                "description": t(lang, "soul.voice_prompt_description"),
            },
        },
        "required": ["action"],
    }


def handle(agent, args: dict) -> dict:
    """Handle soul tool — inquiry and config are agent-invocable; flow
    is mechanical and fires on a wall-clock timer (cannot be invoked
    manually).
    """
    action = args.get("action", "")

    if action == "flow":
        return {
            "error": (
                f"soul flow fires automatically every {agent._soul_delay}s. "
                "It cannot be invoked manually. Use inquiry for on-demand "
                "reflection, or config to change the cadence."
            ),
        }

    if action == "inquiry":
        inquiry = args.get("inquiry", "")
        if not isinstance(inquiry, str) or not inquiry.strip():
            return {"error": "inquiry is required — what do you want to reflect on?"}

        agent._log("soul_inquiry", inquiry=inquiry.strip()[:200])

        result = soul_inquiry(agent, inquiry.strip())

        if result:
            agent._persist_soul_entry(result, mode="inquiry")
            agent._log("soul_inquiry_done")
            return {"status": "ok", "voice": result["voice"]}
        else:
            agent._log("soul_inquiry_done")
            return {"status": "ok", "voice": "(silence)"}

    if action == "config":
        return _handle_config(agent, args)

    if action == "voice":
        return _handle_voice(agent, args)

    return {
        "error": (
            f"Unknown soul action: {action}. Use inquiry, config, voice, "
            "or wait for flow (mechanical)."
        )
    }


def _handle_config(agent, args: dict) -> dict:
    """Handle action='config' — adjust soul flow knobs.

    Accepts any subset of: delay_seconds, consultation_interval,
    consultation_past_count. Validates each provided field, updates live
    state, restarts the wall-clock timer if delay changed, persists to
    init.json. Returns old and new values for every field that was
    actually changed (untouched fields are absent from the response).
    """
    provided: dict = {}
    if "delay_seconds" in args:
        provided["delay_seconds"] = args["delay_seconds"]
    if "consultation_interval" in args:
        provided["consultation_interval"] = args["consultation_interval"]
    if "consultation_past_count" in args:
        provided["consultation_past_count"] = args["consultation_past_count"]
    if not provided:
        return {
            "error": (
                "config requires at least one of: delay_seconds, "
                "consultation_interval, consultation_past_count."
            ),
        }

    new_values: dict = {}
    old_values: dict = {}

    if "delay_seconds" in provided:
        raw = provided["delay_seconds"]
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return {"error": f"delay_seconds must be a number, got {type(raw).__name__}."}
        if v != v:  # NaN
            return {"error": "delay_seconds must be a finite number, got NaN."}
        if v < SOUL_DELAY_MIN_SECONDS:
            return {
                "error": (
                    f"delay_seconds must be at least {SOUL_DELAY_MIN_SECONDS}s "
                    f"(got {v}). Below this, consultation cost dominates "
                    "the main agent loop."
                ),
            }
        old_values["delay_seconds"] = float(agent._soul_delay)
        agent._soul_delay = v
        new_values["delay_seconds"] = v

    if "consultation_interval" in provided:
        raw = provided["consultation_interval"]
        try:
            v = int(raw)
        except (TypeError, ValueError):
            return {"error": f"consultation_interval must be an integer, got {type(raw).__name__}."}
        if v < 0:
            return {"error": f"consultation_interval must be >= 0 (got {v})."}
        if v > 0 and v < CONSULTATION_INTERVAL_MIN:
            return {
                "error": (
                    f"consultation_interval must be 0 (off) or >= "
                    f"{CONSULTATION_INTERVAL_MIN} (got {v}). Below the floor, "
                    "fires would dominate the agent's main work."
                ),
            }
        old_values["consultation_interval"] = int(getattr(agent._config, "consultation_interval", 0))
        agent._config.consultation_interval = v
        new_values["consultation_interval"] = v

    if "consultation_past_count" in provided:
        raw = provided["consultation_past_count"]
        try:
            v = int(raw)
        except (TypeError, ValueError):
            return {"error": f"consultation_past_count must be an integer, got {type(raw).__name__}."}
        if v < CONSULTATION_PAST_COUNT_MIN or v > CONSULTATION_PAST_COUNT_MAX:
            return {
                "error": (
                    f"consultation_past_count must be in "
                    f"[{CONSULTATION_PAST_COUNT_MIN}, {CONSULTATION_PAST_COUNT_MAX}] "
                    f"(got {v}). 0 = insights-only fires; cap protects against "
                    "fan-out cost and chat-history bloat."
                ),
            }
        old_values["consultation_past_count"] = int(getattr(agent._config, "consultation_past_count", 2))
        agent._config.consultation_past_count = v
        new_values["consultation_past_count"] = v

    # Restart the wall-clock timer if delay changed (or if any change
    # happened — restarting on every config call keeps the cadence in
    # sync without surprising drift; cheap operation).
    if "delay_seconds" in new_values:
        try:
            if not agent._shutdown.is_set():
                agent._start_soul_timer()
        except Exception as e:
            agent._log("soul_config_restart_failed", error=str(e)[:200])

    persist_error = _persist_soul_config(agent, new_values)

    log_kw: dict = {"old": old_values, "new": new_values}
    if persist_error:
        log_kw["persist_error"] = persist_error
    agent._log("soul_config", **log_kw)

    return {
        "status": "ok",
        "old": old_values,
        "new": new_values,
    }


def _handle_voice(agent, args: dict) -> dict:
    """Handle action='voice' — read, switch preset, or set a custom soul
    voice prompt. The agent owns this — it chooses how its own inner
    voice sounds in soul-flow consultations.

    Modes:
      - bare (no ``set``): read current voice, list available presets,
        return the resolved system prompt as it stands.
      - ``set=<preset>`` (one of SOUL_VOICE_BUILTINS): switch to a
        built-in profile. Clears any prior custom prompt.
      - ``set="custom"``: requires a non-empty ``prompt`` field
        (length-capped at SOUL_VOICE_PROMPT_MAX). Stores the prompt and
        marks the voice as custom; takes effect on the next consultation
        fire.

    Persists changes to manifest.soul in init.json so they survive
    restart.
    """
    set_to = args.get("set")
    current_voice = getattr(agent._config, "soul_voice", "inner") or "inner"
    current_prompt = getattr(agent._config, "soul_voice_prompt", "") or ""

    # ---- Read mode ----------------------------------------------------
    if set_to is None:
        try:
            resolved = _build_soul_system_prompt(agent, kind="insights")
        except Exception as e:
            resolved = f"<resolution failed: {e!s}>"
        return {
            "status": "ok",
            "current": current_voice,
            "available": list(SOUL_VOICE_BUILTINS),
            "prompt": resolved,
            **(
                {"custom_prompt": current_prompt}
                if current_voice == "custom" and current_prompt
                else {}
            ),
        }

    # ---- Validate set value ------------------------------------------
    if not isinstance(set_to, str):
        return {"error": f"set must be a string, got {type(set_to).__name__}."}
    set_to = set_to.strip()
    if not set_to:
        return {"error": "set is empty — pass a profile name or 'custom'."}

    valid = set(SOUL_VOICE_BUILTINS) | {"custom"}
    if set_to not in valid:
        return {
            "error": (
                f"Unknown voice profile: {set_to!r}. "
                f"Valid: {sorted(SOUL_VOICE_BUILTINS) + ['custom']}."
            ),
        }

    # ---- Custom mode --------------------------------------------------
    new_prompt = ""
    if set_to == "custom":
        raw_prompt = args.get("prompt")
        if not isinstance(raw_prompt, str) or not raw_prompt.strip():
            return {
                "error": (
                    "set='custom' requires a non-empty 'prompt' field — "
                    "this is the system prompt your soul-flow voice will "
                    "use. Speak as the soul; describe how you want to be "
                    "framed when reading your own diary."
                ),
            }
        if len(raw_prompt) > SOUL_VOICE_PROMPT_MAX:
            return {
                "error": (
                    f"prompt is too long ({len(raw_prompt)} chars). "
                    f"Maximum is {SOUL_VOICE_PROMPT_MAX}."
                ),
            }
        new_prompt = raw_prompt

    # ---- Apply --------------------------------------------------------
    old_voice = current_voice
    old_prompt = current_prompt
    agent._config.soul_voice = set_to
    # Switching away from custom clears the stored custom prompt so it
    # does not silently re-activate later. Switching INTO custom stores
    # the new prompt; switching between built-in presets clears.
    agent._config.soul_voice_prompt = new_prompt if set_to == "custom" else ""

    persist_error = _persist_soul_voice(
        agent, voice=set_to, voice_prompt=agent._config.soul_voice_prompt,
    )

    log_kw: dict = {"old_voice": old_voice, "new_voice": set_to}
    if old_voice == "custom" or set_to == "custom":
        # Only log prompt previews when custom is involved; preset
        # switches don't carry meaningful prompt content.
        log_kw["old_prompt_chars"] = len(old_prompt)
        log_kw["new_prompt_chars"] = len(new_prompt)
    if persist_error:
        log_kw["persist_error"] = persist_error
    agent._log("soul_voice", **log_kw)

    return {
        "status": "ok",
        "old": old_voice,
        "new": set_to,
    }


def _persist_soul_config(agent, new_values: dict) -> str | None:
    """Write changed soul knobs into manifest.soul.* in init.json.

    Maps:
      - delay_seconds            -> manifest.soul.delay
      - consultation_interval    -> manifest.soul.consultation_interval
      - consultation_past_count  -> manifest.soul.consultation_past_count

    Atomic via temp-file-then-rename. Returns ``None`` on success, or a
    short error string on failure (caller logs it; runtime state is
    unaffected).
    """
    import json
    import os
    from pathlib import Path

    init_path: Path = agent._working_dir / "init.json"
    try:
        with init_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return f"init.json not found at {init_path}"
    except Exception as e:
        return f"failed to read init.json: {e!s}"[:200]

    if not isinstance(data, dict):
        return "init.json root is not an object"
    manifest = data.setdefault("manifest", {})
    if not isinstance(manifest, dict):
        return "manifest is not an object"
    soul_block = manifest.get("soul")
    if not isinstance(soul_block, dict):
        soul_block = {}
        manifest["soul"] = soul_block

    if "delay_seconds" in new_values:
        soul_block["delay"] = new_values["delay_seconds"]
    if "consultation_interval" in new_values:
        soul_block["consultation_interval"] = new_values["consultation_interval"]
    if "consultation_past_count" in new_values:
        soul_block["consultation_past_count"] = new_values["consultation_past_count"]

    return _atomic_write_init(init_path, data)


def _persist_soul_voice(agent, *, voice: str, voice_prompt: str) -> str | None:
    """Write soul voice profile + (optional) custom prompt into
    manifest.soul in init.json.

    Maps:
      - voice         -> manifest.soul.voice
      - voice_prompt  -> manifest.soul.voice_prompt (only when voice == "custom";
                          deleted from manifest when switching back to a preset
                          to avoid stale prompts re-activating later)

    Atomic via temp-file-then-rename. Returns ``None`` on success, or a
    short error string on failure (caller logs it; runtime state is
    unaffected).
    """
    import json
    from pathlib import Path

    init_path: Path = agent._working_dir / "init.json"
    try:
        with init_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return f"init.json not found at {init_path}"
    except Exception as e:
        return f"failed to read init.json: {e!s}"[:200]

    if not isinstance(data, dict):
        return "init.json root is not an object"
    manifest = data.setdefault("manifest", {})
    if not isinstance(manifest, dict):
        return "manifest is not an object"
    soul_block = manifest.get("soul")
    if not isinstance(soul_block, dict):
        soul_block = {}
        manifest["soul"] = soul_block

    soul_block["voice"] = voice
    if voice == "custom":
        soul_block["voice_prompt"] = voice_prompt
    else:
        soul_block.pop("voice_prompt", None)

    return _atomic_write_init(init_path, data)


def _atomic_write_init(init_path, data) -> str | None:
    """Write ``data`` to ``init_path`` via temp-file-then-rename.

    Used by both _persist_soul_config and _persist_soul_voice. Returns
    ``None`` on success or a short error string on failure.
    """
    import json
    import os

    tmp_path = init_path.with_suffix(init_path.suffix + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_path, init_path)
        return None
    except Exception as e:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return f"failed to write init.json: {e!s}"[:200]


def _send_with_timeout(agent, session, content: str):
    """Send with timeout using a daemon thread. Returns response or None.

    Uses a daemon thread so it dies with the process — no orphaned threads.
    """
    import threading
    timeout = agent._config.retry_timeout
    result_box: list = []
    error_box: list = []

    def _worker():
        try:
            result_box.append(session.send(content))
        except Exception as e:
            error_box.append(e)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=timeout)

    if t.is_alive():
        # Timed out — thread is daemon, will die with process
        agent._log("soul_whisper_error", error=f"LLM call timed out after {timeout}s")
        return None
    if error_box:
        agent._log("soul_whisper_error", error=str(error_box[0])[:200])
        return None
    return result_box[0] if result_box else None


def _build_soul_system_prompt(agent, kind: str = "inquiry") -> str:
    """Build the soul session's system prompt.

    kind:
        "inquiry"           — synchronous mirror session (soul_inquiry).
                              Frames the consultee as a deep copy of the
                              current agent answering a question. Uses the
                              static soul.system_prompt key.
        "insights" / "past" — soul-flow consultation voices. Both resolve
                              from the agent's chosen voice profile (the
                              system prompt is now kind-agnostic — the
                              per-fire cue text differentiates whose diary
                              the consultee is reading).

    For the flow kinds, profile resolution is:
      - ``soul_voice == "custom"`` → use ``_config.soul_voice_prompt`` verbatim
      - any other profile name    → look up ``soul.voice.<name>.prompt`` from i18n
      - missing/empty profile     → fall back to "inner"

    Legacy ``agent._soul_flow_prompt`` operator override (if set) still
    wins over voice profile — kept for backward compatibility with hosts
    that injected a persona before the voice action existed.
    """
    custom_legacy = getattr(agent, "_soul_flow_prompt", "")
    if custom_legacy and kind in ("insights", "past"):
        return custom_legacy

    from ..i18n import t
    if kind == "inquiry":
        return t(agent._config.language, "soul.system_prompt")

    voice = getattr(agent._config, "soul_voice", "inner") or "inner"
    if voice == "custom":
        prompt = getattr(agent._config, "soul_voice_prompt", "") or ""
        if prompt.strip():
            return prompt
        # Empty custom prompt → fall back to inner so the agent never
        # runs a consultation with no system prompt at all.
        voice = "inner"
    return t(agent._config.language, f"soul.voice.{voice}.prompt")


def _render_current_diary(agent) -> str:
    """Concatenate the agent's diary entries from logs/events.jsonl.

    Diary is the agent's free-text output across turns — logged via
    ``self._log("diary", text=response.text)`` in the runtime loop. Each
    entry is one turn. Entries are bounded by molt cadence (a molt resets
    the chat and starts a new diary stream), so the cumulative length is
    self-limited; we don't cap it explicitly here. The window-fit step
    further down applies a token budget at the seeded-interface level,
    not the cue.

    Returns a single string with paragraph-break separators, or empty
    string if the log is missing/unreadable/empty.
    """
    import json
    log_path = agent._working_dir / "logs" / "events.jsonl"
    if not log_path.is_file():
        return ""
    parts: list[str] = []
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("type") != "diary":
                    continue
                text = rec.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
    except Exception:
        return ""
    return "\n\n".join(parts)


def _write_soul_tokens(agent, response) -> None:
    """Append a soul-tagged token-ledger entry for a consultation or
    inquiry LLM call. Best-effort — failures are silently swallowed so
    a ledger hiccup does not break the cadence."""
    u = response.usage
    if not (u.input_tokens or u.output_tokens or u.thinking_tokens or u.cached_tokens):
        return
    try:
        from ..token_ledger import append_token_entry
        ledger_path = agent._working_dir / "logs" / "token_ledger.jsonl"
        model = getattr(agent.service, "model", None)
        endpoint = getattr(agent.service, "_base_url", None)
        append_token_entry(
            ledger_path,
            input=u.input_tokens, output=u.output_tokens,
            thinking=u.thinking_tokens, cached=u.cached_tokens,
            model=model, endpoint=endpoint,
            extra={"source": "soul"},
        )
    except Exception:
        pass


def soul_inquiry(agent, question: str) -> dict | None:
    """Inquiry mode — one-shot mirror session with cloned conversation.

    Clones the agent's conversation (thinking + diary only, no tool
    calls/results), sends the question. Fresh session each time.
    """
    from ..llm.interface import ChatInterface, TextBlock, ThinkingBlock

    cloned = ChatInterface()

    if agent._chat is not None:
        for entry in agent._chat.interface.entries:
            if entry.role == "system":
                continue
            stripped: list = []
            for block in entry.content:
                if isinstance(block, (TextBlock, ThinkingBlock)):
                    stripped.append(block)
            if stripped:
                if entry.role == "assistant":
                    cloned.add_assistant_message(stripped)
                else:
                    cloned.add_user_blocks(stripped)

    system_prompt = _build_soul_system_prompt(agent)
    system_prompt += "\n\nYou have no tools. Respond with plain text only. Never output tool calls or XML tags."

    try:
        session = agent.service.create_session(
            system_prompt=system_prompt,
            tools=None,
            model=agent._config.model or agent.service.model,
            thinking="high",
            tracked=False,
            interface=cloned,
        )
    except Exception as e:
        agent._log("soul_whisper_error", error=str(e)[:200])
        return None

    response = _send_with_timeout(agent, session, question)
    if not response or not response.text:
        return None

    _write_soul_tokens(agent, response)

    return {
        "prompt": question,
        "voice": response.text,
        "thinking": response.thoughts or [],
    }


# ---------------------------------------------------------------------------
# Past-self consultation — the new soul flow
#
# Replaces timer-driven flow with per-turn-cadence past-self consultation.
# Snapshots written by psyche._write_molt_snapshot are the substrate; this
# layer reads them, runs M = 1+K consultations in parallel (1 insights
# against the current self, K random past-snapshot consultations), bundles
# the voices into a single synthetic (assistant{tool_call}, user{tool_result})
# pair, and hands it off to base_agent's tc_inbox with replace_in_history=True.
#
# The single-slot invariant in chat history is enforced at drain time: any
# prior soul.flow pair already in ChatInterface.entries is removed before
# the new pair is appended. This keeps voices "drifting" naturally from
# tail toward prefix as subsequent turns extend history past them, which
# preserves provider prompt cache through the voice's lifetime.
# ---------------------------------------------------------------------------


def _load_snapshot_interface(path):
    """Load a snapshot file written by psyche._write_molt_snapshot and
    return a tools-stripped ``ChatInterface``, or None on any failure.

    Schema check: payload must carry an integer ``schema_version`` and
    a list ``interface`` of entry dicts compatible with
    ``ChatInterface.from_dict``. Any failure (missing file, bad JSON,
    schema mismatch, malformed entries) returns None — the caller skips
    that snapshot and proceeds with whatever else loaded.

    Tool stripping happens at two layers:

    1) Block layer — every ``ToolCallBlock`` and ``ToolResultBlock`` is
       filtered out of non-system entries. Only ``TextBlock`` and
       ``ThinkingBlock`` survive; entries that become empty after
       stripping are dropped. The consultation session is created with
       ``tools=None``, so leaving these blocks would orphan tool_results
       and confuse strict providers.

    2) Schema layer — the past self's frozen tool schema list (stored on
       the system entry as ``_tools`` and surfaced via the rebuilt
       interface's ``_current_tools``) is zeroed. The system entry's
       *prose text* is preserved — it describes the past self's identity
       and what it could do, and that's what we want the past life to
       remember itself by — but the machine-readable schema list, which
       adapters would otherwise pick up and send on the wire, is wiped.
       This guarantees the consultation can never accidentally re-surface
       the past life's tools.
    """
    import json
    from pathlib import Path
    from ..llm.interface import (
        ChatInterface,
        TextBlock,
        ThinkingBlock,
    )

    try:
        p = Path(path)
        if not p.is_file():
            return None
        raw = p.read_text(encoding="utf-8")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return None
        if not isinstance(payload.get("schema_version"), int):
            return None
        entries = payload.get("interface")
        if not isinstance(entries, list):
            return None
        loaded = ChatInterface.from_dict(entries)
    except Exception:
        return None

    # Strip every tool block from the loaded interface. Walk entries,
    # keep only TextBlock+ThinkingBlock content for non-system entries,
    # drop entries that empty out. System entries pass through with their
    # *text* preserved (frozen identity / job description) but their
    # ``tools`` schema list zeroed — the consultation must never re-emit
    # the past life's tool schemas on the wire.
    #
    # Round-trip via entry.to_dict() so id/timestamp/etc. are preserved
    # (ChatInterface.from_dict requires those keys). For non-system entries
    # we replace the "content" list with the stripped block dicts.
    stripped_dicts: list[dict] = []
    for entry in loaded.entries:
        d = entry.to_dict()
        if entry.role == "system":
            d.pop("tools", None)  # drop frozen tool schema list
            stripped_dicts.append(d)
            continue
        kept_blocks = [b for b in entry.content if isinstance(b, (TextBlock, ThinkingBlock))]
        if not kept_blocks:
            continue
        d["content"] = [b.to_dict() for b in kept_blocks]
        stripped_dicts.append(d)
    try:
        rebuilt = ChatInterface.from_dict(stripped_dicts)
    except Exception:
        return None

    # Defense in depth: ChatInterface.from_dict copies the system entry's
    # _tools onto iface._current_tools (interface.py:853). We just popped
    # "tools" from the dict so the entry-level _tools is already None, but
    # zero current_tools explicitly in case future code paths surface it.
    rebuilt._current_tools = None
    for e in rebuilt.entries:
        if e.role == "system":
            e._tools = None
    return rebuilt


def _fit_interface_to_window(iface, target_tokens: int):
    """Tail-trim a ChatInterface to fit within ``target_tokens`` while
    preserving tool-call/tool-result pairing invariants.

    Strategy: walk entries from the end backward, accumulating until the
    next addition would exceed ``target_tokens``. The resulting "kept
    suffix" must start on a clean boundary — never with a user{tool_result}
    whose matching assistant{tool_call} has been dropped. If the natural
    cutoff falls mid-pair, walk one more step backward (or forward, if
    that would yield zero entries) until a clean start is reached.

    System entries (typically index 0) are *always* preserved at the head
    of the kept set when present, since they carry the frozen system
    prompt that the snapshot represents. They count toward the budget.

    Returns a fresh ChatInterface containing only the kept entries.
    """
    from ..llm.interface import ChatInterface, ToolCallBlock, ToolResultBlock

    if target_tokens <= 0:
        return ChatInterface.from_dict([])

    entries = list(iface.entries)
    if not entries:
        return ChatInterface.from_dict([])

    # Already fits — return as-is (clone via to_dict round-trip so the
    # caller can mutate the trimmed copy without affecting the source).
    current = iface.estimate_context_tokens()
    if current <= target_tokens:
        return ChatInterface.from_dict(iface.to_dict())

    # Identify a leading system entry (preserve it).
    head_system = []
    body_start = 0
    if entries[0].role == "system":
        head_system = [entries[0]]
        body_start = 1

    # Walk body from tail backward to find the largest suffix that fits.
    # Build a dict-list of (head_system + suffix) and ask the live
    # ChatInterface for an accurate token count each time.
    head_dicts = [e.to_dict() for e in head_system]
    body = entries[body_start:]
    body_dicts = [e.to_dict() for e in body]

    kept_suffix_start = len(body)  # start with empty suffix
    for i in range(len(body) - 1, -1, -1):
        candidate_dicts = head_dicts + body_dicts[i:]
        probe = ChatInterface.from_dict(candidate_dicts)
        if probe.estimate_context_tokens() > target_tokens:
            break
        kept_suffix_start = i

    # Adjust kept_suffix_start to land on a clean boundary: if the entry
    # at kept_suffix_start is a user-role with only ToolResultBlocks,
    # find the matching tool_call earlier in the body and either include
    # the call (drop kept_suffix_start by 1) or drop the result entry
    # (raise kept_suffix_start by 1). The simpler safe move is the
    # latter — drop the orphaned tool_result entry from the head of the
    # suffix.
    while kept_suffix_start < len(body):
        entry = body[kept_suffix_start]
        if entry.role != "user":
            break
        # If every block in this user entry is a ToolResultBlock, it's a
        # candidate orphan. Check whether its tool_call ids appear in the
        # already-kept suffix (they can only appear earlier than us — by
        # construction the matching call would be just before, so it's
        # been excluded by the cutoff).
        if all(isinstance(b, ToolResultBlock) for b in entry.content):
            # Orphan tool_result with no preceding tool_call in the kept
            # suffix. Drop it and keep walking forward in case the next
            # entry is also orphan.
            kept_suffix_start += 1
            continue
        break

    final_body = body[kept_suffix_start:]
    if not final_body and not head_system:
        # Nothing fits at all — return an empty interface rather than
        # something malformed. Caller treats empty interface as "skip
        # this consultation."
        return ChatInterface.from_dict([])

    final_dicts = head_dicts + [e.to_dict() for e in final_body]
    return ChatInterface.from_dict(final_dicts)


def _kind_for_source(source: str) -> str:
    """Map a consultation source label to its prompt kind."""
    if source == "insights":
        return "insights"
    return "past"


def _build_consultation_cue(agent, kind: str, diary: str) -> str:
    """Localized cue prompt for a consultation voice.

    insights — current self stepping back to look at its own diary.
    past     — past self handed the future self's diary as context.

    Both kinds inject the diary at ``{diary}``. If the diary is empty
    (no diary entries logged yet), the cue still works — the placeholder
    becomes "(no diary yet)" for legibility.
    """
    from ..i18n import t
    key = (
        "soul.consultation_cue_insights"
        if kind == "insights"
        else "soul.consultation_cue_past"
    )
    template = t(agent._config.language, key)
    body = diary if diary else "(no diary yet)"
    try:
        return template.format(diary=body)
    except Exception:
        # If the i18n string lacks {diary} for some reason, append the
        # diary block manually rather than failing the whole consultation.
        return f"{template}\n\n{body}"


def _run_consultation(agent, iface, source: str) -> dict | None:
    """Run one consultation against a seeded ChatInterface.

    Creates a one-shot tools-less session over the given interface and
    sends a kind-specific cue:
      - insights: stepped-back read of the current self's diary
      - past:     past self being handed the future self's diary

    Returns ``{"source": source, "voice": str, "thinking": list}`` or
    None on any failure (load issue, timeout, empty response).

    Window-fit happens here: tail-trim to 0.8 × the consulting model's
    context window. The consulting model is the same model that runs the
    main agent chat (``agent.service``), so the budget is read from the
    main chat's ``context_window()`` (or ``config.context_limit`` if the
    user pinned a smaller cap). Same pattern as meta_block.py and
    session.py use everywhere else in the kernel.
    """
    if iface is None or not iface.entries:
        return None

    # Read the consulting model's context window the same way the rest of
    # the kernel does. If the agent hasn't built its main chat yet (early
    # boot), conservatively assume 200k — every modern provider supports
    # at least that and it leaves room for the system prompt + cue.
    window = None
    if getattr(agent, "_chat", None) is not None:
        try:
            window = agent._chat.context_window()
        except Exception:
            window = None
    if window is None:
        window = int(getattr(agent._config, "context_limit", None) or 200_000)
    target = max(1, int(window * 0.8))
    fitted = _fit_interface_to_window(iface, target)
    if not fitted.entries:
        return None

    kind = _kind_for_source(source)
    system_prompt = _build_soul_system_prompt(agent, kind=kind)
    system_prompt += "\n\nYou have no tools. Respond with plain text only. Never output tool calls or XML tags."

    try:
        session = agent.service.create_session(
            system_prompt=system_prompt,
            tools=None,
            model=agent._config.model or agent.service.model,
            thinking="high",
            tracked=False,
            interface=fitted,
        )
    except Exception as e:
        try:
            agent._log("consultation_session_failed", source=source, error=str(e)[:200])
        except Exception:
            pass
        return None

    diary = _render_current_diary(agent)
    cue = _build_consultation_cue(agent, kind, diary)
    response = _send_with_timeout(agent, session, cue)
    if not response or not response.text:
        return None

    try:
        _write_soul_tokens(agent, response)
    except Exception:
        pass

    return {
        "source": source,
        "voice": response.text,
        "thinking": response.thoughts or [],
    }


def _list_snapshot_paths(agent):
    """Return a list of pathlib.Path entries for all snapshot files in
    <workdir>/history/snapshots/, or [] if the directory does not exist."""
    from pathlib import Path
    snapshots_dir = agent._working_dir / "history" / "snapshots"
    if not snapshots_dir.is_dir():
        return []
    try:
        return sorted(snapshots_dir.glob("snapshot_*.json"))
    except Exception:
        return []


def _clone_current_chat_for_insights(agent):
    """Build a tools-stripped ChatInterface clone of the agent's current
    chat. Same pattern as soul_inquiry: keep TextBlock + ThinkingBlock
    only, skip ToolCallBlock + ToolResultBlock entirely (the consultation
    session is tools-less).
    """
    from ..llm.interface import ChatInterface, TextBlock, ThinkingBlock

    cloned = ChatInterface()
    if agent._chat is None:
        return cloned

    for entry in agent._chat.interface.entries:
        if entry.role == "system":
            continue
        stripped = []
        for block in entry.content:
            if isinstance(block, (TextBlock, ThinkingBlock)):
                stripped.append(block)
        if not stripped:
            continue
        if entry.role == "assistant":
            cloned.add_assistant_message(stripped)
        else:
            try:
                cloned.add_user_blocks(stripped)
            except Exception:
                # Pending-tool-call guard tripped on the clone — skip
                # this entry rather than crash. Should be very rare since
                # we already stripped tool blocks.
                continue

    return cloned


def _run_consultation_batch(agent) -> list[dict]:
    """Run one full consultation fire: 1 insights + K past-snapshot
    consultations in parallel. Returns the list of surviving voices
    (failed/timed-out consultations are filtered out).
    """
    import random
    import threading

    K = max(0, int(getattr(agent._config, "consultation_past_count", 2)))

    # Build work items.
    work: list[tuple[str, "ChatInterface"]] = []
    insights_iface = _clone_current_chat_for_insights(agent)
    if insights_iface.entries:
        work.append(("insights", insights_iface))

    # Sample K snapshot paths; load each.
    paths = _list_snapshot_paths(agent)
    if paths and K > 0:
        sampled = random.sample(paths, min(K, len(paths)))
        for path in sampled:
            iface = _load_snapshot_interface(path)
            if iface is None or not iface.entries:
                try:
                    agent._log("consultation_load_failed", path=str(path))
                except Exception:
                    pass
                continue
            # source label encodes molt_count + ts when parseable from filename
            source = f"snapshot:{path.stem}"
            work.append((source, iface))

    if not work:
        return []

    # Run all consultations in parallel daemon threads with a barrier.
    results: list[dict | None] = [None] * len(work)

    def worker(idx: int, source: str, iface) -> None:
        try:
            results[idx] = _run_consultation(agent, iface, source)
        except Exception as e:
            try:
                agent._log("consultation_thread_error",
                           source=source, error=str(e)[:200])
            except Exception:
                pass
            results[idx] = None

    threads: list[threading.Thread] = []
    for idx, (source, iface) in enumerate(work):
        t = threading.Thread(
            target=worker, args=(idx, source, iface),
            daemon=True,
            name=f"consult-w-{idx}-{source[:20]}",
        )
        threads.append(t)
        t.start()

    timeout = float(getattr(agent._config, "retry_timeout", 300.0)) * 2.0
    for t in threads:
        t.join(timeout=timeout)

    voices = [r for r in results if r is not None and r.get("voice")]
    return voices


def build_consultation_pair(agent, voices: list[dict], tc_id: str | None = None):
    """Build a synthetic (ToolCallBlock, ToolResultBlock) pair carrying
    the bundled consultation voices. The result content includes an
    appendix_note framing the voices as advisory and ephemeral.

    ``tc_id`` may be supplied by the caller — useful when the fire layer
    wants the chat-history call_id to match the soul_flow.jsonl fire_id
    (cross-reference between logs and chat). If omitted, a fresh id is
    generated.
    """
    import secrets
    import time
    from ..llm.interface import ToolCallBlock, ToolResultBlock
    from ..i18n import t as _t

    if not tc_id:
        tc_id = f"tc_{int(time.time())}_{secrets.token_hex(2)}"
    call = ToolCallBlock(id=tc_id, name="soul", args={"action": "flow"})

    # Strip the thinking block from the wire payload — it inflates tokens
    # without adding readable signal at the consumption site (the agent
    # main turn). Keep the source label and the voice text only.
    rendered_voices = [
        {"source": v["source"], "voice": v["voice"]}
        for v in voices
        if v.get("voice")
    ]
    payload = {
        "appendix_note": _t(agent._config.language, "soul.appendix_note"),
        "voices": rendered_voices,
    }
    result = ToolResultBlock(id=tc_id, name="soul", content=payload)
    return call, result
