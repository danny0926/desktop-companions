Nino desktop dialogue agent
===========================

Entry points (Python 3.10+, standard library only)

    from desktop_pet.agent import AgentService
    from desktop_pet.contracts import AgentRequest

    agent = AgentService(
        config_path="config/desktop_pet_character.json",
        data_dir="outputs/pet-agent-v1/state",
    )
    reply = agent.generate_reply(AgentRequest("turn-001", "今天工作好累", "my-chat"))
    reply.to_dict()                 # JSON-ready UI payload
    reply.to_voice_request("0")     # text_ja, caption_ja, turn_id, segment_id

generate_reply is synchronous. An asyncio gateway should use
await asyncio.to_thread(agent.generate_reply, request). One AgentService serializes
its model turns. Use a fresh turn_id for every new input. Retrying the same
conversation_id + turn_id returns the existing reply without another model call;
using that id with different input raises ValueError.

User-authored roleplay actions

    from desktop_pet.agent import UserAction, parse_user_action, format_user_action

    action = parse_user_action("（摸頭）")
    pointer_action = UserAction("headpat", "輕輕摸頭", source="mouse")
    text = format_user_action(pointer_action)
    reply = agent.generate_reply(AgentRequest("touch-001", text, "my-chat"))

Typed wrappers （...）, (...), *...*, /action ... and exact short aliases are
recognized. Ordinary questions mentioning an action remain dialogue. A custom
description is limited to 80 characters. format/parse preserve text/mouse/menu
source. The dialogue context marks an action as an actual user interaction,
without inventing an automatic user message or a new animation. Only the
character's Japanese response goes to speech_text_ja; stage directions and
action JSON are rejected as speech. Stored input remains the user's original.

The character config currently contains six official TBS profile facts and
twelve separately attributed community facts paraphrased from Moegirlpedia.
Both pages were read in the original workspace on 2026-11-01. Private rendered
browser captures are excluded; source attribution is retained in character config. The prompt includes at most 20 facts
within 2200 characters, with official facts first and unread sources excluded.
The adult desktop scenario and relationship choice remain application fiction.

Command line from the repository root

    python -m desktop_pet.agent --health
    python -m desktop_pet.agent --text "おかえり、二乃。"
    python -m desktop_pet.agent --memories
    python -m desktop_pet.agent --sources

Without --text or another action, stdin accepts one AgentRequest JSON object per
line and stdout returns one reply per line. Errors are structured JSON on stderr.

The default uses the installed official Codex CLI and its existing ChatGPT login.
The first actual completion was successful with gpt-6-luna on 2026-11-01. The
observed command took about 7.88 seconds including Python and CLI startup. That
is one observed turn, not a sustained latency promise. No LLM is loaded on this
machine and no lab LLM service is required.

Subscription provider configuration

    provider.mode = "codex_subscription"
    provider.model = "gpt-6-luna"
    provider.codex.executable = "codex"
    provider.timeout_seconds = 45

The official CLI handles authentication; this package does not read or export
credentials. `codex login status` must report ChatGPT authentication. If needed,
run `codex login` yourself and choose ChatGPT. API-key login is not accepted by
this provider. The child process removes inherited OPENAI_API_KEY/CODEX_API_KEY
and requires forced_login_method="chatgpt". No automatic model purchase, key
setup, credit purchase, or provider switch occurs.

Each turn runs `codex exec` with a fresh temporary cwd, --ignore-user-config,
--ephemeral, --sandbox read-only, --json, and --output-schema. Project instructions
are capped at zero bytes; tools, apps, delegation, hooks, browsing and installed
plugin access are disabled for the invocation. A small replacement instruction
file defines the dialogue task. User input goes through UTF-8 stdin, without a
shell. The application waits at most its configured subprocess timeout and then
terminates that child process tree; completion errors are exposed to the UI.
Temporary request files are cleaned up by the application. CLI authentication and
its own operational files remain managed by the CLI. The provider does not change
the user's Codex configuration.

Health and switching

agent.health() returns provider_mode, model, authenticated/auth_method,
llm_ready (null until a successful completion), last_error, last_generation_ms,
provider_run event types and usage when available, and memory_count.
agent.reload_provider() reloads the character config and checks the provider.

Stop and shutdown

    from desktop_pet.agent import ProviderCancelled

    agent.cancel("turn-001")   # only this matching in-flight turn
    agent.cancel()             # all currently active/queued turns
    agent.close()              # idempotent shutdown; rejects new generate requests

cancel does not acquire the generation lock. Every request has a separate
cancellation event. The Codex provider associates that event and turn id with
its own Popen handle, terminates that exact child process tree, and waits for it
to exit. A delayed cancellation cannot stop another turn. A process-creation
barrier covers cancel/close racing Popen, before its process handle is published.
Pending requests can be cancelled before they acquire the generation lock.

An interrupted generate_reply raises ProviderCancelled (a ProviderError
subclass). Treat it as a normal user stop in the UI. A cancelled result is not
saved as a generated conversation turn. Shutdown should call close before the
UI process exits, even if its dialogue thread is a daemon. After close, a new
generate_reply or reload_provider raises ProviderUnavailable.

Cancelling the dialogue provider does not stop TTS/audio playback; the shell
must cancel that queue independently. HTTP/local-model adapters are excluded.

Independent proactive system-event turns

    reply = agent.generate_proactive_reply(
        decision.event,
        turn_id="proactive-unique-turn",
        conversation_id="desktop-default",
        expires_at=decision.expires_at,
        max_reply_chars=decision.max_reply_chars,
    )

This is a distinct path, not an AgentRequest with an invented user utterance.
It accepts behavior.ProactiveEvent or contracts.ProactiveRequest. A serialized
ProactiveRequest is also accepted. Pass the event object, not only the result of
event.model_context(): the latter deliberately omits the observed_at clock needed
for local freshness checks. observed_at and expires_at are time.monotonic()
values from the same process as the UI/agent, matching ProactiveDecision. Do not
convert them to/from UTC or Unix timestamps.

Only return_from_idle and focus_break with coarse activity category, numeric
durations, and win32/injected observation sources are accepted. Arbitrary window
titles, keyboard/screen contents or free-form instructions are not forwarded.
The renderer receives the current event as a labelled system message. A format
repair also uses a system message; neither creates a synthetic user turn. The
default limit is 36 Japanese characters, with the same caption/acting separation.
Injected observations remain marked as supplied scenarios, not Win32 evidence.

The behavior controller owns all cadence, quiet mode, busy-state and cooldown
decisions. This API does not poll, schedule or call a model for raw observations.
The caller must check decision.request_speech and should_deliver before starting
generation, then recheck should_deliver immediately before playback. Manual chat
should cancel the proactive turn with agent.cancel(turn_id), in addition to
invalidating the behavior decision and cancelling its TTS/playback queue.

    if not controller.should_deliver(decision, busy=current_busy,
                                     now=time.monotonic()):
        agent.record_delivery(reply.turn_id, conversation_id="desktop-default",
                              status="discarded")

After actual delivery, call record_delivery with the same conversation_id. Full
delivery can use status="delivered"; an interrupted utterance should provide
heard_text and status="interrupted". Without this acknowledgement, a proactive
reply does not enter recent conversation context. Normal manual chat must use
that same conversation_id to see the delivered remark in its next context.

SQLite proactive_turns stores event provenance independently from real user
conversation_turns and user_memories. Delivered proactive context consists of a
system provenance note plus the assistant words actually delivered. An observation
never becomes a fabricated user message or an automatically stored user fact.
Interrupted replies retain only heard words, with the original subtitle removed.
Cancelling/discarding an undelivered reply clears its text and preserves only its
event/status tombstone. A late delivery ACK cannot resurrect that text. close()
also redacts outstanding undelivered proactive replies.

Repeating the same event+turn returns its cached eligible reply without another
model call. Rebinding an event to another turn, cancelled/failed events, expired
events, or a reply exceeding a newly shorter limit raise ProactiveEventRejected
(a ProviderCancelled subclass); the UI may quietly skip these. Unknown kinds,
sources or malformed clock values raise ValueError. The effective expiration is
the earlier of decision.expires_at and observed_at+60 seconds by default. The
event deadline also bounds time spent waiting for the generation lock or model.
Offline demo mode does not fabricate a proactive model response.

The injected-provider verification and source notes are in
outputs/pet-agent-v1/proactive-verification.json and proactive-research.json.
Real Codex proactive inference and shell/voice integration remain a separate
coordinator smoke check; the injection checks do not establish those results.

Optional named environment overrides:
    DESKTOP_PET_LLM_MODE
    DESKTOP_PET_LLM_MODEL
    DESKTOP_PET_LLM_TIMEOUT

Set provider.mode="offline_demo" to inspect authored lines without a model.
Every such reply has provider_mode="offline_demo" and its subtitle begins with
an explicit offline-demo label. `/demo angry light firm` previews the independent
attitude/intensity/delivery controls. `/demo shy` is also valid. These fixed
original lines are for wiring and acting preview, not general conversation.

This independent app supports subscription dialogue and explicit offline_demo
only. It does not probe local servers or switch to paid API services.

Character, acting and sources

config/desktop_pet_character.json owns the official core, separately attributed
community supplement, original style examples, and this app's fictional scene.
The default relationship_mode is familiar_companion. romantic_partner is an
optional local-scene setting. Neither is represented as an official story fact.
Community facts are injected only when their source is marked verified. The
Moegirl page returned a JavaScript challenge during research, so it contributes
no facts in this version. list_sources() exposes all attribution and the scene.

The model returns Japanese spoken text, optional Traditional Chinese subtitles,
attitude, intensity and delivery. The backend derives caption_ja, expression and
motion from allowlists. It ignores a model's proposed captions/actions. Japanese
speech must contain kana and exclude JSON/control tags, speaker prefixes and
parenthesized stage directions. Invalid structured output gets at most one
format repair within the shared request deadline. A sprite renderer can fall
back to neutral for any expression absent from its asset pack.

Only speech_text_ja (or VoiceRequest.text_ja) is spoken. caption_ja is a Japanese
performance description for TTS. subtitle_zh is displayed text. Never append
control fields, subtitles or captions to spoken text. Anger does not force a
strong intensity, and strong emotion does not automatically mean shouting.

Memory and conversation

SQLite uses separate user_memories and conversation_turns tables scoped by
character_id. Recent dialogue is bounded to eight turns and 5,000 characters in
the default prompt; the stored journal retains at most 80 turns per conversation.
Long-term recall uses words/CJK bigrams plus a small preference/profile bias,
bounded to six entries and 1,800 characters. There is no vector DB, embedding
model, LLM extraction service, or summarization loop in this first version.

    item = agent.remember_fact("使用者偏好無糖紅茶", key="user.drink", kind="preference")
    agent.list_memories()
    agent.delete_memory(item["id"])

Reusing a memory key replaces that item while preserving its id. Entries include
source, source_turn_id and timestamps. User/editor writes cannot change official
character data. No model-generated inference is automatically saved as a fact.
`/remember ...` or `記住：...` saves explicit text; `/memories` lists entries and
`/forget <id>` deletes one. Their acknowledgements use provider_mode=local_memory
and need no LLM. Memory commands are omitted from subsequent dialogue context.
For a full list, use list_memories() instead of the short spoken-command listing.

Deleting a fact removes that long-term entry. Existing ordinary chat turns can
still contain what the user said. clear_conversation(conversation_id) deletes the
conversation journal separately and leaves saved facts intact.

Generated replies initially enter working context because their text is visible
in the chat UI. A gateway should revise interrupted/discarded speech:

    agent.record_delivery("turn-001", conversation_id="my-chat",
                          heard_text="実際に聞こえた部分", status="interrupted")
    agent.record_delivery("turn-002", conversation_id="my-chat", status="discarded")

This corrects next-turn context; it does not stop TTS/audio itself. The gateway
still owns stale-turn suppression and playback cancellation. Demonstration
dialogue is excluded when a real provider builds its next conversation prompt.

Evidence and limits

outputs/pet-agent-v1/verification.json records the single real Codex completion
and one focused pass for the JSON/voice contract, ten acting choices, SQLite
restart/update/delete/isolation, memory-command retries, interrupted speech, and
control-tag rejection. Optional HTTP servers were not available and were not
tested for real inference. Forced live rate limits/timeouts, long-term persona
quality and sustained latency were not tested. See research.json for the primary
sources and the design decisions adopted without copying upstream code.

outputs/pet-agent-v1/cancellation-verification.json records a focused additional
check for active cancellation, late/wrong ids, Popen startup races and shutdown.
It used four controlled local Python child processes and reaped all four without
contacting a model. Run only that check with:
    python outputs/pet-agent-v1/verify_agent.py --cancellation-only
