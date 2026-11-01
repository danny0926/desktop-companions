"""One conversation worker, latest-turn wins, and no Tk calls off the UI thread."""
from __future__ import annotations

from dataclasses import dataclass, field
from collections import OrderedDict
import queue
import re
import threading
import time
import uuid
from typing import Any, Callable

from desktop_pet.contracts import AgentReply, AgentRequest, ManualImage, VoiceRequest


def speech_segments(text: str, limit: int = 80) -> list[str]:
    """Keep sentence endings and enforce the local TTS request limit."""
    parts = re.findall(r"[^。！？!?\n]+[。！？!?]*|[。！？!?]+", text)
    split_parts: list[str] = []
    for sentence in parts:
        sentence = sentence.strip()
        while len(sentence) > limit:
            boundary = max(sentence.rfind(mark, 0, limit) for mark in ("、", "，", ",", " ", "；"))
            end = boundary + 1 if boundary >= limit // 3 else limit
            split_parts.append(sentence[:end].strip())
            sentence = sentence[end:].strip()
        if sentence:
            split_parts.append(sentence)
    result: list[str] = []
    pack_limit = min(limit, 24)
    for part in split_parts:
        if result and len(result[-1]) < 8 and len(result[-1]) + len(part) <= pack_limit:
            result[-1] += part
        else:
            result.append(part)
    return result


@dataclass
class Turn:
    # turn_id is a unique shell/voice operation, even when replaying the same
    # Agent reply. It must never be reused as a cancellation target on retry.
    turn_id: str
    input_text: str
    speak: bool
    profile: str = "quality_40"
    agent_turn_id: str = ""
    retry_reply: AgentReply | None = None
    replay_items: list[dict] | None = None
    proactive_decision: Any = None
    delivery_guard: Callable | None = None
    finished: bool = False
    audio_prepared: bool = False
    playback_active: bool = False
    draining: bool = False
    screen_observation: Any = None
    images: tuple[ManualImage, ...] = ()
    cancelled: threading.Event = field(default_factory=threading.Event)


class ConversationController:
    def __init__(self, config: dict, emit: Callable[[dict], None], *, agent=None, voice=None, screen_capture=None):
        self.config = config
        self.emit = emit
        self.agent = agent
        self.voice = voice
        self._screen_capture = screen_capture
        self._services_lock = threading.Lock()
        self._condition = threading.Condition()
        self._pending: Turn | None = None
        self._active: Turn | None = None
        self._operations: OrderedDict[str, str] = OrderedDict()
        self._replay_operations: set[str] = set()
        self._prewarm_token: str | None = None
        self._prewarm_request: str | None = None
        self._closing = False
        self.closed = threading.Event()
        self._deliveries: queue.Queue = queue.Queue()
        threading.Thread(target=self._record_deliveries, name="pet-delivery", daemon=True).start()
        self._worker = threading.Thread(target=self._run, name="pet-conversation", daemon=True)
        self._worker.start()

    def _get_agent(self):
        with self._services_lock:
            if self._closing:
                raise RuntimeError("The pet services are closed.")
            if self.agent is None:
                from desktop_pet.agent import AgentService
                self.agent = AgentService(self.config["agent_config"], data_dir=self.config["agent_data_dir"])
            return self.agent

    def _get_voice(self):
        with self._services_lock:
            if self._closing:
                raise RuntimeError("The pet services are closed.")
            if self.voice is None:
                from desktop_pet.voice import VoiceService
                self.voice = VoiceService(self.config["voice_config"])
            return self.voice

    def refresh_health(self):
        def check():
            for name, getter in (("agent", self._get_agent), ("voice", self._get_voice)):
                if self._closing:
                    return
                try:
                    self.emit({"type": "health", "service": name, "value": getter().health()})
                except Exception as exc:
                    self.emit({"type": "health", "service": name,
                               "value": {"status": "unavailable", "error": str(exc)}})
        threading.Thread(target=check, name="pet-health", daemon=True).start()

    def refresh_voice_health(self):
        if self.voice is not None and not self._closing:
            try:
                self.emit({"type": "health", "service": "voice", "value": self.voice.health()})
            except Exception as exc:
                self.emit({"type": "diagnostic", "error": str(exc)})

    def prewarm(self):
        token = uuid.uuid4().hex
        with self._condition:
            old_request = self._prewarm_request
            self._prewarm_token, self._prewarm_request = token, None
        self._cancel_prewarm(old_request)
        def prepare():
            self.emit({"type": "prewarm", "prewarm_id": token, "status": "loading"})
            try:
                voice = self._get_voice()
                with self._condition:
                    if self._prewarm_token != token or self._closing:
                        return
                request_id = voice.submit_prewarm()
                with self._condition:
                    current = self._prewarm_token == token and not self._closing
                    if current:
                        self._prewarm_request = request_id
                if not current:
                    self._cancel_prewarm(request_id)
                    return
                result = voice.wait(request_id, timeout=float(self.config.get("voice_timeout_seconds", 190)))
                self.emit({"type": "prewarm", "prewarm_id": token, "status": result.get("status"), "result": result})
            except Exception as exc:
                self.emit({"type": "prewarm", "prewarm_id": token, "status": "failed", "error": str(exc)})
            finally:
                with self._condition:
                    if self._prewarm_token == token:
                        self._prewarm_request = None
            self.refresh_voice_health()
        threading.Thread(target=prepare, name="pet-prewarm", daemon=True).start()
        return token

    def _cancel_prewarm(self, request_id: str | None):
        if not request_id or self.voice is None:
            return
        def cancel():
            try:
                self.voice.cancel(request_id=request_id)
            except Exception as exc:
                self.emit({"type": "diagnostic", "error": f"prewarm.cancel: {exc}"})
        threading.Thread(target=cancel, name="pet-cancel-prewarm", daemon=True).start()

    def set_profile(self, profile: str):
        if self.voice is not None and hasattr(self.voice, "set_profile"):
            self.voice.set_profile(profile)

    def start(self, text: str, *, speak: bool = True, profile: str = "quality_40",
              images: tuple[ManualImage, ...] = ()) -> str:
        operation = uuid.uuid4().hex
        return self._schedule(Turn(operation, text, speak, profile, agent_turn_id=operation, images=tuple(images)))

    def retry_voice(self, reply: AgentReply | dict, *, profile: str = "quality_40") -> str:
        if not isinstance(reply, AgentReply):
            reply = AgentReply.from_dict(reply)
        if reply.provider_mode not in {"codex_subscription", "local_memory"}:
            raise ValueError("Voice retry requires an actual saved Agent reply.")
        return self._schedule(Turn(uuid.uuid4().hex, "", True, profile,
                                   agent_turn_id=reply.turn_id, retry_reply=reply))

    def replay_audio(self, record: dict, playlist: list[dict]) -> str:
        """Schedule original WAVs under a new cancellable local operation."""
        if not playlist or any(item.get("status") != "replay" or not item.get("wav_path") for item in playlist):
            raise ValueError("Replay requires existing recorded WAV pieces.")
        turn = Turn(uuid.uuid4().hex, "", True, record.get("profile", "quality_40"),
                    agent_turn_id=record["agent_turn_id"], replay_items=list(playlist))
        operation = self._schedule(turn)
        # Local files must not wait for the cancelled Agent/TTS worker to return.
        turn.audio_prepared = True
        for index, item in enumerate(turn.replay_items):
            if not self.current(operation):
                break
            self._event(turn, "audio", result=item, text=item.get("text_spoken_ja", ""),
                        last=index == len(turn.replay_items) - 1)
        self._event(turn, "finished", voice=True)
        turn.finished = True
        return operation

    def start_proactive(self, decision, *, should_deliver: Callable, speak: bool = True,
                        profile: str = "quality_40") -> str | None:
        if not decision.request_speech or not should_deliver(decision):
            return None
        operation = uuid.uuid4().hex
        return self._schedule(Turn(operation, "", speak, profile, agent_turn_id=operation,
                                   proactive_decision=decision, delivery_guard=should_deliver))

    def _schedule(self, turn: Turn) -> str:
        with self._condition:
            if self._closing:
                raise RuntimeError("The pet services are closed.")
            old = self._active
            if turn.proactive_decision is not None and old and not old.finished and not old.cancelled.is_set():
                return None  # A low-priority observation cannot replace manual work.
            old_prewarm = self._prewarm_request
            self._prewarm_token = self._prewarm_request = None
            if old:
                old.cancelled.set()
                old.screen_observation = None
                old.images = ()
            self._active = turn
            self._pending = None if turn.replay_items is not None else turn
            self._operations[turn.turn_id] = turn.agent_turn_id
            if turn.replay_items is not None:
                self._replay_operations.add(turn.turn_id)
            while len(self._operations) > 32:
                expired, _ = self._operations.popitem(last=False)
                self._replay_operations.discard(expired)
            self._condition.notify()
        self._cancel_voice(old.turn_id if old and old.replay_items is None else None)
        self._cancel_prewarm(old_prewarm)
        self._cancel_agent(old.agent_turn_id if old and old.retry_reply is None and old.replay_items is None else None)
        return turn.turn_id

    def current(self, turn_id: str) -> bool:
        with self._condition:
            return bool(not self._closing and self._active and
                        self._active.turn_id == turn_id and not self._active.cancelled.is_set())

    def mark_playback(self, turn_id: str, active: bool):
        with self._condition:
            if self._active is not None and self._active.turn_id == turn_id:
                self._active.playback_active = active

    def complete_delivery(self, turn_id: str):
        """Release pixels once the UI has no queued or playing file left."""
        with self._condition:
            if self._active is not None and self._active.turn_id == turn_id:
                self._active.screen_observation = None

    def allow_delivery(self, turn_id: str, *, continuing=False) -> bool:
        with self._condition:
            turn = self._active
            if not self.current(turn_id) or turn is None:
                return False
        if turn.proactive_decision is None:
            return True
        try:
            continuing = continuing and turn.playback_active
            if turn.screen_observation is not None:
                # A foreground change invalidates unseen context immediately.
                # Only age is relaxed for the WAV that has already started.
                age_limit = (max(60, time.monotonic() - turn.screen_observation.observed_at + 1)
                             if continuing else 60)
                if self._screen_capture is None or not self._screen_capture.is_current(
                        turn.screen_observation, max_age_seconds=age_limit):
                    return False
            return ((continuing or time.monotonic() <= turn.proactive_decision.expires_at) and
                    bool(turn.delivery_guard and turn.delivery_guard(turn.proactive_decision,
                         audio_prepared=turn.audio_prepared, continuing=continuing)))
        except Exception:
            return False

    def cancel_proactive(self, reason: str = "manual_interaction"):
        with self._condition:
            turn = self._active
            if turn is None or turn.proactive_decision is None:
                return
            turn.cancelled.set()
            turn.screen_observation = None
            self._active = None
            if self._pending is turn:
                self._pending = None
            self._condition.notify()
        self._cancel_voice(turn.turn_id)
        self._cancel_agent(turn.agent_turn_id)
        self.emit({"type": "proactive_discarded", "turn_id": turn.turn_id, "reason": reason})

    def _discard_proactive(self, turn: Turn, reason: str):
        if turn.playback_active and self.allow_delivery(turn.turn_id, continuing=True):
            # Expiry forbids starting another segment. Finish the WAV already
            # accepted by Windows; external manual/fullscreen/lock guards above
            # still cancel immediately and never enter this draining state.
            turn.draining = turn.finished = True
            self._cancel_voice(turn.turn_id)
            self.emit({"type": "proactive_draining", "turn_id": turn.turn_id, "reason": reason})
            return
        self.record_delivery(turn.turn_id, "", "discarded")
        turn.finished = True
        if self.current(turn.turn_id):
            self.cancel_proactive(reason)
        else:
            self.emit({"type": "proactive_discarded", "turn_id": turn.turn_id, "reason": reason})

    def stop(self):
        with self._condition:
            old = self._active
            old_prewarm = self._prewarm_request
            self._prewarm_token = self._prewarm_request = None
            if old:
                old.cancelled.set()
                old.screen_observation = None
                old.images = ()
            self._active = self._pending = None
            self._condition.notify()
        self._cancel_voice(old.turn_id if old and old.replay_items is None else None)
        self._cancel_prewarm(old_prewarm)
        self._cancel_agent(old.agent_turn_id if old and old.retry_reply is None and old.replay_items is None else None)

    def _cancel_agent(self, turn_id: str | None):
        if self.agent is None or not turn_id or not hasattr(self.agent, "cancel"):
            return
        def cancel():
            try:
                value = self.agent.cancel(turn_id=turn_id)
                self.emit({"type": "cancel_requested", "turn_id": turn_id, "service": "agent", "value": value})
            except Exception as exc:
                self.emit({"type": "diagnostic", "error": f"agent.cancel: {exc}"})
        threading.Thread(target=cancel, name="pet-cancel-agent", daemon=True).start()

    def _cancel_voice(self, turn_id: str | None):
        # The facade's cancellation may notify its subprocess. Never wait on Tk.
        if self.voice is not None and turn_id:
            def cancel():
                try:
                    result = self.voice.cancel(turn_id=turn_id)
                    self.emit({"type": "cancel_requested", "turn_id": turn_id, "value": result})
                except Exception as exc:
                    self.emit({"type": "diagnostic", "error": f"voice.cancel: {exc}"})
            threading.Thread(target=cancel, name="pet-cancel", daemon=True).start()

    def record_delivery(self, turn_id: str, text: str, status: str = "delivered"):
        with self._condition:
            if turn_id in self._replay_operations:
                return  # Listening again never changes the original delivery memory.
        if self.agent is None or not hasattr(self.agent, "record_delivery"):
            return
        with self._condition:
            agent_turn_id = self._operations.get(turn_id, turn_id)
        self._deliveries.put((agent_turn_id, text, status))

    def _record_deliveries(self):
        while True:
            turn_id, text, status = self._deliveries.get()
            try:
                self.agent.record_delivery(turn_id, conversation_id=self.config["conversation_id"],
                                           heard_text=text, status=status)
            except Exception as exc:
                self.emit({"type": "diagnostic", "error": f"record_delivery: {exc}"})
            finally:
                self._deliveries.task_done()

    def close(self):
        self.stop()
        with self._condition:
            self._closing = True
            self._condition.notify()
        def cleanup():
            try:
                with self._services_lock:
                    if self.voice is not None:
                        self.voice.close()
                    if self.agent is not None and hasattr(self.agent, "close"):
                        self.agent.close()
                    if self._screen_capture is not None:
                        self._screen_capture.close()
                self._deliveries.join()
            finally:
                self.closed.set()
        # Non-daemon: keep Python alive long enough to reap only our own ML child.
        threading.Thread(target=cleanup, name="pet-close-services", daemon=False).start()

    def _event(self, turn: Turn, kind: str, **fields):
        if self.current(turn.turn_id) and not turn.draining:
            self.emit({"type": kind, "turn_id": turn.turn_id,
                       "agent_turn_id": turn.agent_turn_id, "voice_retry": turn.retry_reply is not None,
                       "replay": turn.replay_items is not None,
                       "proactive": turn.proactive_decision is not None, **fields})

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closing or self._pending is not None)
                if self._closing:
                    return
                turn, self._pending = self._pending, None
            try:
                if not self.allow_delivery(turn.turn_id):
                    if turn.proactive_decision is not None:
                        self._discard_proactive(turn, "ineligible_before_generation")
                    continue
                if turn.proactive_decision is not None:
                    decision = turn.proactive_decision
                    if getattr(decision.event.kind, "value", decision.event.kind) == "screen_observation":
                        self._event(turn, "phase", value="observing")
                        if self._screen_capture is None:
                            from desktop_pet.observation import ScreenCapture
                            self._screen_capture = ScreenCapture()
                        turn.screen_observation = self._screen_capture.capture(
                            scope=decision.event.screen_scope, cancel_event=turn.cancelled,
                            expires_at=decision.expires_at)
                        if turn.screen_observation is None or not self.allow_delivery(turn.turn_id):
                            self._discard_proactive(turn, "screen_unavailable_or_changed")
                            continue
                        self._event(turn, "screen_observation", observation=turn.screen_observation.metadata())
                    agent = self._get_agent()
                    self._event(turn, "phase", value="thinking")
                    visual = ({"screenshot": turn.screen_observation} if turn.screen_observation is not None else {})
                    reply = agent.generate_proactive_reply(
                        decision.event, turn_id=turn.agent_turn_id,
                        conversation_id=self.config["conversation_id"], expires_at=decision.expires_at,
                        max_reply_chars=decision.max_reply_chars,
                        **visual,
                    )
                    if not self.allow_delivery(turn.turn_id):
                        self._discard_proactive(turn, "ineligible_after_generation")
                        continue
                elif turn.retry_reply is None:
                    agent = self._get_agent()
                    self._event(turn, "phase", value="thinking")
                    visual = {"images": turn.images} if turn.images else {}
                    reply = agent.generate_reply(AgentRequest(turn.agent_turn_id, turn.input_text, self.config["conversation_id"]),
                                                 **visual)
                    turn.images = ()
                    if not self.current(turn.turn_id):
                        self.record_delivery(turn.turn_id, "", "discarded")
                        self.emit({"type": "stale_discarded", "turn_id": turn.turn_id, "stage": "agent"})
                        continue
                else:
                    # No Agent construction, generation, or duplicate transcript
                    # event is needed to retry the exact saved speech/caption.
                    reply = turn.retry_reply
                if reply.provider_mode not in {"codex_subscription", "offline_demo", "local_memory"}:
                    raise RuntimeError("Unexpected dialogue provider; configure the signed-in Codex subscription.")
                if turn.retry_reply is None:
                    self._event(turn, "reply", reply=reply.to_dict())
                if not turn.speak:
                    self.record_delivery(turn.turn_id, reply.speech_text_ja)
                    self._event(turn, "finished", voice=False)
                    continue
                voice = self._get_voice()
                if not self.allow_delivery(turn.turn_id):
                    if turn.proactive_decision is not None:
                        self._discard_proactive(turn, "ineligible_before_voice")
                    continue
                voice.begin_turn(turn.turn_id)
                self._event(turn, "phase", value="synthesizing")
                limit = min(int(getattr(voice, "config", {}).get("max_text_chars", 80)),
                            int(self.config.get("max_segment_chars", 48)))
                segments = speech_segments(reply.speech_text_ja, limit)
                for index, text in enumerate(segments):
                    if not self.allow_delivery(turn.turn_id):
                        if turn.proactive_decision is not None:
                            self._discard_proactive(turn, "ineligible_before_segment")
                        break
                    request = VoiceRequest(text, reply.caption_ja, turn.turn_id, str(index))
                    result = voice.synthesize(request, timeout=float(self.config.get("voice_timeout_seconds", 190)), profile=turn.profile)
                    if result.get("status") == "generated" and result.get("playable") and result.get("wav_path"):
                        turn.audio_prepared = True
                    if not self.allow_delivery(turn.turn_id):
                        if turn.proactive_decision is not None:
                            self._discard_proactive(turn, "ineligible_after_synthesis")
                        self.emit({"type": "stale_discarded", "turn_id": turn.turn_id, "stage": "voice"})
                        break
                    self._event(turn, "voice_result", result=result)
                    if result.get("status") != "generated" or not result.get("playable") or not result.get("wav_path"):
                        self._event(turn, "voice_error", result=result)
                        break
                    self._event(turn, "audio", result=result, text=text, last=index == len(segments) - 1)
                self._event(turn, "finished", voice=True)
            except Exception as exc:
                if turn.proactive_decision is not None:
                    self._discard_proactive(turn, f"{type(exc).__name__}: {exc}")
                else:
                    self._event(turn, "error", error=f"{type(exc).__name__}: {exc}")
            finally:
                turn.images = ()
                turn.finished = True
