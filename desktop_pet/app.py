"""A real Windows desktop pet, rendered by Tk with a small native shell."""
from __future__ import annotations

from collections import deque
from dataclasses import replace
import json
import math
from pathlib import Path
import queue
import time
import tkinter as tk
from tkinter import ttk
import traceback

from .agent.actions import UserAction, format_user_action, parse_user_action
from .shell.controller import ConversationController
from .shell.behavior import BehaviorBridge
from .shell.carry import CarryMotion
from .shell.interaction import HeadpatGesture, head_hit
from desktop_pet.behavior import BusyState, MotionState, ObservationSource, ProactiveEventKind
from .shell.playback import WavePlayer
from .shell.recovery import load_last_reply, store_last_reply
from .shell.replay import ReplayStore
from .shell.resources import resource_details, resource_issue, resource_status
from .shell.sprites import EXPRESSION_ANIMATION, SpriteAtlas
from .shell.win32 import (NativeShell, capture_png, clamp_position, cursor_position,
                           hwnd_of, monitor_layout, move_window, passive_window, window_rect, work_area)


BG = "#FCFAFF"
INK = "#352B45"
MUTED = "#83778E"
ACCENT = "#76529B"
PALE = "#EEE6F6"
CHROMA = "#FF00FE"


class PetApplication:
    def __init__(self, root: tk.Tk, config: dict, instance, *, smoke: bool = False):
        self.root, self.config, self.instance = root, config, instance
        self.smoke = smoke
        self.events: queue.Queue = queue.Queue()
        self.state_dir = Path(config["state_dir"])
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.state_dir / "window.json"
        self.log_path = Path(config["event_log"])
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.preferences = {"topmost": True, "subtitles": True, "speak": True, "size": "small",
                            "skin": config.get("default_skin", "pixel_v2"), "pixel_v3_migrated": False,
                            "roaming": True, "seated": False, "proactive": "occasional",
                            "screen_enabled": config.get("screen_observation_enabled", True),
                            "screen_frequency": "normal", "screen_scope": "active_window",
                            "idle_actions": True, "return_after_idle_seconds": 180}
        self.position = None
        self._draft_text = ""
        if self.state_path.is_file():
            try:
                saved = json.loads(self.state_path.read_text(encoding="utf-8-sig"))
                self.preferences.update({key: saved[key] for key in self.preferences if key in saved})
                if all(isinstance(saved.get(key), int) for key in ("x", "y")):
                    self.position = saved["x"], saved["y"]
                if isinstance(saved.get("chat_draft"), str):
                    self._draft_text = saved["chat_draft"]
            except (OSError, ValueError, TypeError):
                pass
        if config.get("no_voice"):
            self.preferences["speak"] = False
        self.closing = False
        self.exit_code = 0
        self.chat: tk.Toplevel | None = None
        self.settings: tk.Toplevel | None = None
        self.bubble: tk.Toplevel | None = None
        self._bubble_turn_id = None
        self.transcript = None
        self.input_box = None
        self._transcript_parts: list[tuple] = []
        self.voice_library = None
        self._voice_library_list = None
        self._voice_library_ids: list[str] = []
        self._turn_is_replay = False
        self.health: dict = {}
        self.diagnostics: list[str] = []
        self.turn_id: str | None = None
        self._heard: list[str] = []
        self._reply: dict | None = None
        self._last_reply: dict | None = None
        self._last_reply_profile = "quality_40"
        self._turn_profile = "quality_40"
        self._current_input_text: str | None = None
        self._retry_buttons: list = []
        self._synthesis_pending = False
        self._voice_failed = False
        self._turn_complete = False
        self._turn_speak = True
        self._prewarm_pending = False
        self._prewarm_id: str | None = None
        self._audio_queue: deque = deque()
        self.player = WavePlayer()
        self._animation = "idle"
        self._animation_start = time.monotonic()
        self._expression_until = 0.0
        self._reply_action_until = 0.0
        self._motion = "idle"
        self._motion_start = 0.0
        self._drag = None
        self._dragged = False
        self._drag_press_at = 0.0
        self._carry = CarryMotion()
        self._carry_grip = (0.0, 0.0)
        self._carry_sample = None
        self._touch_gesture: HeadpatGesture | None = None
        self._touch_reaction_until = 0.0
        self._touch_animation = "happy"
        self._touch_is_presentation = False
        self._last_touch_reply_at = float("-inf")
        self._turn_is_touch = False
        self._last_frame = None
        self._turn_is_proactive = False
        self._active_proactive_decision = None
        self._last_manual_at = float("-inf")
        self._last_typing_at = float("-inf")
        self._chat_input_focused = False
        self._last_motion_state = None
        self._health_log_signatures = {}
        self._activity_signature = None
        self._proactive_status_signature = None
        self._last_proactive_status_log_at = float("-inf")
        self._project_root = Path(__file__).resolve().parents[1]
        self.replays = ReplayStore(self.state_dir / "voice-library", workspace=self._project_root)
        if not smoke:
            try:
                self.replays.import_events((self.log_path, self.log_path.parent / "events.jsonl",
                                            self.log_path.parent / "final-integration-events.jsonl"))
            except (OSError, ValueError) as exc:
                self.diagnostics.append(f"voice history: {exc}")
        self._skin_paths = {"pixel_v2": self._project_root / "desktop_pet/assets/nino-pixel-v2/manifest.json",
                            "q_v1": self._project_root / "desktop_pet/assets/nino/manifest.json"}
        for name, value in config.get("skins", {}).items():
            path = Path(value)
            self._skin_paths[name] = path if path.is_absolute() else self._project_root / path
        if "pixel_v3" in self._skin_paths and not self._skin_paths["pixel_v3"].is_file():
            self._skin_paths.pop("pixel_v3")
        if "pixel_v3" in self._skin_paths and not self.preferences["pixel_v3_migrated"]:
            if self.preferences["skin"] == "pixel_v2":
                self.preferences["skin"] = "pixel_v3"
            self.preferences["pixel_v3_migrated"] = True
        if self.preferences["skin"] not in self._skin_paths:
            self.preferences["skin"] = "pixel_v2"
        self.status = tk.StringVar(root, "摸摸頭、點身體聊天，或按住身體拖曳。")
        self.subtitle_var = tk.BooleanVar(root, bool(self.preferences["subtitles"]))
        self.speak_var = tk.BooleanVar(root, bool(self.preferences["speak"]))
        self.topmost_var = tk.BooleanVar(root, bool(self.preferences["topmost"]))
        self.clickthrough_var = tk.BooleanVar(root, False)
        self.size_var = tk.StringVar(root, self.preferences["size"])
        self.profile_var = tk.StringVar(root, "quality_40")
        self.skin_var = tk.StringVar(root, self.preferences["skin"])
        self.roaming_var = tk.BooleanVar(root, bool(self.preferences["roaming"]))
        self.seated_var = tk.BooleanVar(root, bool(self.preferences["seated"]))
        self.seat_button_var = tk.StringVar(root, "起來走走" if self.seated_var.get() else "坐在原地")
        self.proactive_var = tk.StringVar(root, self.preferences["proactive"] if self.preferences["proactive"] in {"quiet", "occasional", "lively"} else "occasional")
        self.screen_enabled_var = tk.BooleanVar(root, bool(self.preferences["screen_enabled"]))
        self.screen_frequency_var = tk.StringVar(root, self.preferences["screen_frequency"] if self.preferences["screen_frequency"] in {"less", "normal", "more"} else "normal")
        self.screen_scope_var = tk.StringVar(root, self.preferences["screen_scope"] if self.preferences["screen_scope"] in {"active_window", "active_screen"} else "active_window")
        self.idle_actions_var = tk.BooleanVar(root, bool(self.preferences["idle_actions"]))
        self.return_idle_var = tk.IntVar(root, self.preferences["return_after_idle_seconds"] if self.preferences["return_after_idle_seconds"] in {180, 300} else 180)
        self.companion_settings = None
        self._set_theme()
        root.withdraw()
        root.title("二乃像素桌寵")
        root.configure(bg=CHROMA)
        root.overrideredirect(True)
        root.attributes("-transparentcolor", CHROMA)
        root.attributes("-topmost", self.topmost_var.get())
        self.atlas = SpriteAtlas(root, self._skin_paths[self.skin_var.get()], self.size_var.get())
        self._animation = self._posture_animation("idle")
        initial_frame = self.atlas.frame(self._animation, 0)
        self.canvas = tk.Canvas(root, bg=CHROMA, highlightthickness=0, borderwidth=0,
                                width=self.atlas.width + 24, height=self.atlas.height + 20)
        self.canvas.pack()
        self.sprite_item = self.canvas.create_image(12, 10, image=self.atlas.images[initial_frame], anchor="nw")
        for widget in (self.canvas,):
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<ButtonRelease-1>", self._drag_end)
            widget.bind("<Motion>", self._pointer_hover)
            widget.bind("<Button-3>", lambda event: self.show_menu(event.x_root, event.y_root))
            widget.bind("<Escape>", lambda event: self.stop())
        root.bind("<Escape>", lambda event: self.stop())
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.report_callback_exception = self._callback_error
        root.deiconify()
        root.update_idletasks()
        self.native = NativeShell(root, self.atlas.write_icon(self.state_dir), self.events.put, instance.message)
        self.width, self.height = self.atlas.width + 24, self.atlas.height + 20
        behavior_path = Path(config.get("behavior_config", "config/desktop_pet_behavior.json"))
        self.behavior = BehaviorBridge(self.atlas, behavior_path if behavior_path.is_absolute() else self._project_root / behavior_path,
                                        self.events.put, roaming=self.roaming_var.get(), mode=self.proactive_var.get(),
                                        screen_enabled=self.screen_enabled_var.get(), screen_frequency=self.screen_frequency_var.get(),
                                        screen_scope=self.screen_scope_var.get(), idle_actions=self.idle_actions_var.get(),
                                        return_after_idle_seconds=self.return_idle_var.get(), seated=self.seated_var.get())
        self.behavior.update_monitors(monitor_layout())
        if self.position is None:
            self._default_position()
        else:
            self._move(*self.position)
        self.controller = ConversationController(config, self.events.put)
        self._build_menu()
        if not smoke and config.get("restore_last_reply_on_start", True):
            self._restore_last_reply()
        self._log("started", snapshot=self.snapshot())
        root.after(35, self._tick)
        root.after(2000, self._keep_on_screen)
        root.after(1000, self._poll_voice_health)
        if not smoke:
            root.after(100, self.controller.refresh_health)
            if config.get("retry_last_reply_on_start") and self._last_reply:
                root.after(350, self.retry_voice)

    def _set_theme(self):
        self.root.option_add("*Font", ("Microsoft JhengHei UI", 10))
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Pet.TFrame", background=BG)
        style.configure("Pet.TLabel", background=BG, foreground=INK)
        style.configure("Muted.Pet.TLabel", background=BG, foreground=MUTED)
        style.configure("Pet.TButton", padding=(12, 7), background=PALE, foreground=INK, borderwidth=0)
        style.configure("Action.Pet.TButton", padding=(6, 7))
        style.map("Pet.TButton", background=[("active", "#E2D4F0")])
        style.configure("Accent.Pet.TButton", background=ACCENT, foreground="white", padding=(18, 8), borderwidth=0)
        style.map("Accent.Pet.TButton", background=[("active", "#614084")])
        style.configure("Pet.TCheckbutton", background=BG, foreground=INK, padding=(0, 4))
        style.configure("Pet.TRadiobutton", background=BG, foreground=INK, padding=(0, 4))

    def _log(self, kind: str, **fields):
        event = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "monotonic_seconds": time.monotonic(), "event": kind, **fields}
        try:
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass

    def _callback_error(self, kind, value, trace):
        self.exit_code = 1
        error = "".join(traceback.format_exception(kind, value, trace))
        self.diagnostics.append(error)
        self._log("ui_error", error=error)
        self.status.set("操作未完成，請從設定查看狀態。")

    def _default_position(self):
        left, top, right, bottom = work_area(*cursor_position())
        self._move(right - self.width - 36, bottom - self.height - 4)
        if hasattr(self, "behavior"):
            self.behavior.planner.reset()

    def _move(self, x: int, y: int):
        if hasattr(self, "behavior") and self.behavior.topology and self.behavior.topology.nodes:
            x, y = self.behavior.constrain_position((int(x), int(y)))
        else:
            x, y = clamp_position(int(x), int(y), self.width, self.height)
        move_window(self.native.hwnd, round(x), round(y), self.width, self.height, topmost=self.topmost_var.get())

    def _save(self):
        if self.smoke:
            return
        x, y, _, _ = window_rect(self.native.hwnd)
        data = {"x": x, "y": y, "topmost": self.topmost_var.get(), "subtitles": self.subtitle_var.get(),
                "speak": self.speak_var.get(), "size": self.size_var.get(), "skin": self.skin_var.get(),
                "pixel_v3_migrated": self.preferences.get("pixel_v3_migrated", False),
                "roaming": self.roaming_var.get(), "seated": self.seated_var.get(), "proactive": self.proactive_var.get(),
                "screen_enabled": self.screen_enabled_var.get(), "screen_frequency": self.screen_frequency_var.get(),
                "screen_scope": self.screen_scope_var.get(), "idle_actions": self.idle_actions_var.get(),
                "return_after_idle_seconds": self.return_idle_var.get(),
                "chat_draft": self._chat_draft()}
        temporary = self.state_path.with_suffix(".part")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.state_path)

    def _keep_on_screen(self):
        if self.closing:
            return
        try:
            if self.behavior.update_monitors(monitor_layout()):
                self._log("monitor_layout_changed", monitors=self.behavior.monitors)
            x, y, right, bottom = window_rect(self.native.hwnd)
            corrected = self.behavior.constrain_position((x, y))
            if corrected != (x, y) or (right - x, bottom - y) != (self.width, self.height):
                self._move(*corrected)
                self.behavior.planner.reset()
        except OSError as exc:
            self._log("monitor_layout_unavailable", error=str(exc))
        finally:
            self.root.after(2000, self._keep_on_screen)

    def _poll_voice_health(self):
        if self.closing:
            return
        if self.controller.voice is not None:
            self.controller.refresh_voice_health()
        self.root.after(1000, self._poll_voice_health)

    def _pointer_on_head(self, event) -> bool:
        # Tk local coordinates use the same scale as the displayed sprite,
        # including when its native window crosses monitors with different DPI.
        if hasattr(event, "x") and hasattr(event, "y"):
            x, y = event.x, event.y
        else:
            left, top, _, _ = window_rect(self.native.hwnd)
            x, y = event.x_root - left, event.y_root - top
        regions = self.atlas.manifest.get("hit_regions", {})
        region = regions.get("seated_head", regions.get("head")) if self.seated_var.get() else regions.get("head")
        return head_hit(x - 12, y - 10, self.atlas.width, self.atlas.height, region)

    def _pointer_hover(self, event):
        self.canvas.configure(cursor="hand2" if self._pointer_on_head(event) else "fleur")

    def _touch_visual_active(self, now: float) -> bool:
        return self._touch_gesture is not None or now < self._touch_reaction_until

    def _react_to_action(self, action: UserAction, *, now: float | None = None):
        now = time.monotonic() if now is None else now
        animation = {"headpat": "headpat", "cheek_poke": "surprised", "wave": "wave",
                     "hug": "shy", "hand_hold": "shy"}.get(action.kind, "happy")
        self._touch_animation = self._posture_animation(animation)
        self._touch_is_presentation = False
        self._touch_reaction_until = now + 1.6
        self._motion = "idle"
        self._set_animation(self._touch_animation)
        # The visual is local and is visible before any dialogue request starts.
        frame = self.atlas.frame(self._posture_animation(self._touch_animation), (now - self._animation_start) * 1000)
        self.canvas.itemconfigure(self.sprite_item, image=self.atlas.images[frame])
        self.canvas.coords(self.sprite_item, 12, 10)
        self._last_frame = frame

    def _drag_start(self, event):
        self._carry.reset()
        self._carry_sample = None
        self._dragged = False
        if not getattr(event, "state", 0) & 0x0001 and self._pointer_on_head(event):
            self._manual_interaction("headpat", interrupt_touch=False)
            self._drag = None
            now = time.monotonic()
            self._touch_gesture = HeadpatGesture(now, event.x_root, event.y_root)
            self._react_to_action(UserAction("headpat", "輕輕摸頭", "mouse"), now=now)
            return
        self._touch_gesture = None
        self._touch_reaction_until = 0
        x, y, _, _ = window_rect(self.native.hwnd)
        self._drag = event.x_root, event.y_root, x, y
        self._drag_press_at = time.monotonic()

    def _drag_move(self, event):
        if self._touch_gesture is not None:
            self._touch_gesture.move(event.x_root, event.y_root)
            return
        if self._drag is None:
            return
        sx, sy, x, y = self._drag
        dx, dy = event.x_root - sx, event.y_root - sy
        if not self._dragged and abs(dx) + abs(dy) > 5:
            self._manual_interaction("drag")
            self._dragged = True
            scale = float(self.atlas.scale)
            self._carry_grip = ((sx - x - 12) / scale, (sy - y - 10) / scale)
            self._carry.begin((sx, sy), self._drag_press_at, scale=scale)
        if self._dragged:
            self._carry.update((event.x_root, event.y_root), time.monotonic())
            self._move(x + dx, y + dy)

    def _drag_end(self, event):
        if self._touch_gesture is not None:
            gesture, self._touch_gesture = self._touch_gesture, None
            now = time.monotonic()
            action = UserAction("headpat", gesture.description(now, self.atlas.width), "mouse")
            self._react_to_action(action, now=now)
            self.behavior.planner.reset()
            self._record_user_action(action, duration_ms=round((now - gesture.started_at) * 1000),
                                     rub_distance=round(gesture.distance, 1))
            if not self._send_action(action, touch=True, record=False):
                self._log("touch_reply_skipped", source="mouse", reason=self._touch_reply_block_reason(now))
            return
        if self._drag is None:
            return
        dragged = self._dragged
        self._drag = None
        if dragged:
            self._carry.release(time.monotonic())
            self.behavior.planner.reset()
            self._save()
            self._log("dragged", rect=window_rect(self.native.hwnd))
        else:
            self.open_chat()

    def _build_menu(self):
        self.menu = tk.Menu(self.root, tearoff=False, font=("Microsoft JhengHei UI", 10), bg=BG, fg=INK)
        self.menu.add_command(label="和二乃聊天", command=self.open_chat)
        self.menu.add_command(label=self.seat_button_var.get(), command=self.toggle_seated)
        self._seat_menu_index = self.menu.index("end")
        self.interaction_menu = tk.Menu(self.menu, tearoff=False, font=("Microsoft JhengHei UI", 10), bg=BG, fg=INK)
        for label, kind, description in (("摸頭", "headpat", "輕輕摸頭"), ("戳臉", "cheek_poke", "輕輕戳臉"),
                                         ("打招呼", "wave", "向二乃揮手打招呼")):
            self.interaction_menu.add_command(label=label, command=lambda k=kind, d=description: self._menu_action(k, d))
        self.interaction_menu.add_separator()
        self.interaction_menu.add_command(label="自訂動作…", command=self._custom_action)
        self.menu.add_cascade(label="互動", menu=self.interaction_menu)
        self._build_motion_menu()
        self.menu.add_command(label="停止說話  Esc", command=self.stop)
        self.menu.add_command(label="重試語音", command=self.retry_voice, state="disabled")
        self.menu.add_command(label="語音紀錄／收藏", command=self.open_voice_library)
        self.menu.add_separator()
        self.menu.add_checkbutton(label="滑鼠穿透", variable=self.clickthrough_var, command=self.set_click_through)
        self.menu.add_command(label="設定與狀態", command=self.open_settings)
        self.menu.add_command(label="移回螢幕角落", command=self._default_position)
        self.menu.add_separator()
        self.menu.add_command(label="退出二乃", command=self.close)

    def _build_motion_menu(self):
        if hasattr(self, "motion_menu"):
            self.motion_menu.delete(0, "end")
        else:
            self.motion_menu = tk.Menu(self.menu, tearoff=False, font=("Microsoft JhengHei UI", 10), bg=BG, fg=INK)
            self.menu.add_cascade(label="二乃的小動作", menu=self.motion_menu)
        for key, label in (("wave", "揮揮手"), ("stretch", "伸個懶腰"), ("yawn", "打呵欠"), ("adjust_ribbon", "整理髮帶"),
                           ("crossed_arms", "抱臂鬧彆扭"), ("head_tilt", "歪頭湊近"), ("sit_swing", "坐姿擺腿")):
            if key in self.atlas.manifest["animations"]:
                self.motion_menu.add_command(label=label, command=lambda action=key: self._preview_sprite_action(action))
        if self.motion_menu.index("end") is None:
            self.motion_menu.add_command(label="目前外觀沒有額外逐格動作", state="disabled")

    def _preview_sprite_action(self, animation):
        if animation not in self.atlas.manifest["animations"]:
            return
        self._presentation_interaction("sprite_action")
        if animation == "sit_swing" and not self.seated_var.get():
            self.set_seated(True)
        self.behavior.planner.reset()
        self._touch_gesture = None
        self._touch_animation = self._posture_animation(animation)
        self._touch_is_presentation = True
        self._touch_reaction_until = time.monotonic() + self.atlas.animation_duration(animation)
        self._motion = "idle"
        self._set_animation(self._touch_animation)
        self._animation_start = time.monotonic()

    def toggle_seated(self):
        self.set_seated(not self.seated_var.get())

    def set_seated(self, seated: bool):
        self._presentation_interaction("seated_mode")
        self._carry.reset()
        self._carry_sample = None
        self.seated_var.set(bool(seated))
        self.behavior.set_seated(seated)
        self._touch_gesture = None
        self._touch_reaction_until = self._expression_until = self._reply_action_until = 0
        self._motion = "idle"
        self._set_animation("sit_down" if seated else "stand_up")
        label = "起來走走" if seated else "坐在原地"
        self.seat_button_var.set(label)
        self.menu.entryconfigure(self._seat_menu_index, label=label)
        self.status.set("二乃會坐在這裡陪你；聊天、摸頭和主動搭話仍可使用。" if seated else
                        "二乃起來了，會依「自行走動」設定活動。")
        self._save()
        self._refresh_status_panel()
        self._log("seated_mode", enabled=bool(seated), roaming_enabled=self.roaming_var.get())

    def _reply_animation(self, reply, now):
        animation = EXPRESSION_ANIMATION.get(reply.get("expression"), "idle")
        motion = reply.get("motion", "idle")
        self._reply_action_until = 0
        if motion in {"wave", "stretch", "yawn", "adjust_ribbon", "crossed_arms", "head_tilt"} and motion in self.atlas.manifest["animations"]:
            animation = motion
            self._reply_action_until = now + self.atlas.animation_duration(motion)
        self._set_animation(animation)
        if self._reply_action_until:
            self._animation_start = now

    def _update_retry_actions(self):
        state = "normal" if self._last_reply else "disabled"
        self.menu.entryconfigure("重試語音", state=state)
        self._retry_buttons = [button for button in self._retry_buttons if button.winfo_exists()]
        for button in self._retry_buttons:
            button.configure(state=state)

    def _restore_last_reply(self):
        value = load_last_reply(self.config)
        if not value:
            return
        self._last_reply = self._reply = value["reply"]
        self._last_reply_profile = value["profile"]
        self._turn_complete = True
        self.profile_var.set(value["profile"])
        if value.get("input_text"):
            action = parse_user_action(value["input_text"])
            if action:
                self._transcript_parts.append(("action", self._action_speaker(action), action.description))
            else:
                self._transcript_parts.append(("user", "你", value["input_text"]))
        if not self.replays.for_reply(self._reply["turn_id"]):
            self.replays.add_reply(self._reply, self._reply["turn_id"], profile=value["profile"],
                                   created_at=value.get("source_time"))
        self._transcript_parts.append(("reply", "二乃", self._reply["speech_text_ja"], self._reply["turn_id"]))
        if self._reply.get("subtitle_zh"):
            self._transcript_parts.append(("subtitle", "", self._reply["subtitle_zh"]))
        self.status.set("已保留上一則回覆；可重播原音，或按「重試語音」。")
        self._update_retry_actions()
        self._log("reply_restored", agent_turn_id=self._reply["turn_id"], provider_mode=self._reply["provider_mode"])

    def show_menu(self, x: int, y: int):
        try:
            self.menu.tk_popup(x, y)
        finally:
            self.menu.grab_release()

    def set_click_through(self):
        try:
            self.native.set_click_through(self.clickthrough_var.get())
            if self.clickthrough_var.get():
                self.status.set("已開啟穿透；可從托盤或 Ctrl＋Alt＋N 叫回二乃。")
            else:
                self.status.set("已恢復滑鼠操作。")
            self._log("click_through", enabled=self.native.click_through)
        except Exception as exc:
            self.clickthrough_var.set(False)
            self.diagnostics.append(str(exc))
            self.status.set("無法建立恢復入口，穿透保持關閉。")

    def recover(self):
        self.clickthrough_var.set(False)
        self.native.set_click_through(False)
        x, y, _, _ = window_rect(self.native.hwnd)
        self._move(x, y)
        self.open_chat()
        self.status.set("二乃在這裡。")
        self._log("recovered")

    def open_chat(self):
        if self.closing:
            return
        if self.chat and self.chat.winfo_exists():
            self.chat.deiconify()
            self.chat.lift()
            self.input_box.focus_set()
            return
        window = self.chat = tk.Toplevel(self.root)
        window.withdraw()
        window.title("二乃 · 聊天")
        window.configure(bg=BG)
        window.attributes("-topmost", self.topmost_var.get())
        window.minsize(400, 430)
        window.protocol("WM_DELETE_WINDOW", window.withdraw)
        window.bind("<Escape>", lambda event: self.stop())
        window.rowconfigure(1, weight=1)
        window.columnconfigure(0, weight=1)
        header = ttk.Frame(window, style="Pet.TFrame", padding=(20, 16, 20, 10))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="二乃", font=("Microsoft JhengHei UI", 19, "bold"), style="Pet.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="日文對話 · 陪你待一會", style="Muted.Pet.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 0))
        ttk.Button(header, text="語音收藏", style="Action.Pet.TButton", command=self.open_voice_library).grid(row=0, column=1, rowspan=2, padx=(6, 0))
        ttk.Button(header, text="設定", style="Pet.TButton", command=self.open_settings).grid(row=0, column=2, rowspan=2, padx=(6, 0))
        ttk.Button(header, textvariable=self.seat_button_var, style="Action.Pet.TButton",
                   command=self.toggle_seated).grid(row=2, column=2, sticky="e", pady=(8, 0))
        ttk.Label(header, text="摸頭部互動 · 拖身體移動\nShift 拖任意處", style="Muted.Pet.TLabel").grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        body = ttk.Frame(window, style="Pet.TFrame", padding=(18, 0, 10, 0))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.transcript = tk.Text(body, wrap="word", relief="flat", borderwidth=0, bg=BG, fg=INK,
                                   padx=4, pady=12, spacing1=4, spacing3=6, cursor="arrow", state="disabled")
        scrollbar = ttk.Scrollbar(body, command=self.transcript.yview)
        self.transcript.configure(yscrollcommand=scrollbar.set)
        self.transcript.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        for tag, options in {
            "name": {"foreground": ACCENT, "font": ("Microsoft JhengHei UI", 10, "bold")},
            "user": {"foreground": INK, "lmargin1": 10, "lmargin2": 10},
            "action": {"foreground": ACCENT, "lmargin1": 10, "lmargin2": 10},
            "reply": {"foreground": INK, "lmargin1": 10, "lmargin2": 10, "font": ("Yu Gothic UI", 12)},
            "subtitle": {"foreground": MUTED, "lmargin1": 10, "lmargin2": 10},
            "notice": {"foreground": MUTED},
        }.items():
            self.transcript.tag_configure(tag, **options)
        footer = ttk.Frame(window, style="Pet.TFrame", padding=(18, 7, 18, 16))
        footer.grid(row=2, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self.status, wraplength=370, style="Muted.Pet.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 9))
        self.input_box = tk.Text(footer, height=3, wrap="word", relief="solid", borderwidth=1,
                                 bg="white", fg=INK, insertbackground=ACCENT, padx=10, pady=8,
                                 highlightthickness=1, highlightbackground="#E6DCED", highlightcolor=ACCENT)
        self.input_box.grid(row=1, column=0, columnspan=4, sticky="ew")
        self.input_box.bind("<Return>", self._input_return)
        self.input_box.bind("<KeyPress>", self._input_activity, add="+")
        self.input_box.bind("<FocusIn>", lambda event: self._input_focus(True))
        self.input_box.bind("<FocusOut>", lambda event: self._input_focus(False))
        if self._draft_text:
            self.input_box.insert("1.0", self._draft_text)
        shortcuts = ttk.Frame(footer, style="Pet.TFrame")
        shortcuts.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        for column in range(4):
            shortcuts.columnconfigure(column, weight=1, uniform="shortcut")
        for label, kind, description in (("摸頭", "headpat", "輕輕摸頭"), ("戳臉", "cheek_poke", "輕輕戳臉"),
                                         ("打招呼", "wave", "向二乃揮手打招呼")):
            ttk.Button(shortcuts, text=label, width=1, style="Action.Pet.TButton",
                       command=lambda k=kind, d=description: self._menu_action(k, d)).grid(row=0, column=("headpat", "cheek_poke", "wave").index(kind), sticky="ew", padx=(0, 2))
        ttk.Button(shortcuts, text="自訂動作", width=1, style="Action.Pet.TButton", command=self._custom_action).grid(row=0, column=3, sticky="ew")
        ttk.Checkbutton(footer, text="中文字幕", variable=self.subtitle_var, style="Pet.TCheckbutton", command=self._subtitles_changed).grid(row=3, column=0, sticky="w", pady=(8, 0))
        retry = ttk.Button(footer, text="重試語音", command=self.retry_voice, style="Pet.TButton")
        retry.grid(row=3, column=1, padx=(4, 4), pady=(8, 0))
        self._retry_buttons.append(retry)
        ttk.Button(footer, text="停止", command=self.stop, style="Pet.TButton").grid(row=3, column=2, padx=(0, 4), pady=(8, 0))
        ttk.Button(footer, text="傳送", command=self.send_input, style="Accent.Pet.TButton").grid(row=3, column=3, pady=(8, 0))
        self._update_retry_actions()
        if not self._transcript_parts:
            self._append("notice", "", "輸入中文或日文，二乃會用日文回覆。\n動作可輸入（摸頭）、*輕輕拍肩* 或 /action 你的動作。\n摸頭可直接按住角色頭部；按身體拖曳，Shift＋拖曳可從任何位置移動。\nEnter 傳送，Shift＋Enter 換行。")
        else:
            self._render_transcript()
        window.geometry("440x540+0+0")
        window.update_idletasks()
        px, py, pr, pb = window_rect(self.native.hwnd)
        left, top, right, bottom = work_area(px, py, self.width, self.height)
        x = px - 456 if px - 456 >= left else pr + 16
        x, y = clamp_position(x, pb - 540, 440, 540)
        window.deiconify()
        move_window(hwnd_of(window), x, y, 440, 540, topmost=self.topmost_var.get())
        window.lift()
        self.input_box.focus_set()
        self._log("chat_opened")

    def _input_return(self, event):
        if event.state & 0x0001:
            return None
        self.send_input()
        return "break"

    def send_input(self):
        if self.input_box:
            text = self.input_box.get("1.0", "end").strip()
            if self.send_text(text):
                self.input_box.delete("1.0", "end")
                self._draft_text = ""
                self._save()

    def _chat_draft(self) -> str:
        if self.input_box and self.input_box.winfo_exists():
            self._draft_text = self.input_box.get("1.0", "end-1c")
        return self._draft_text

    def _menu_action(self, kind: str, description: str):
        self._send_action(UserAction(kind, description, "menu"))

    def _custom_action(self):
        self.open_chat()
        # Keep an in-progress message; a shortcut must never replace a draft.
        if not self.input_box.get("1.0", "end").strip():
            self.input_box.insert("1.0", "/action ")
        self.input_box.mark_set("insert", "end-1c")
        self.input_box.focus_set()
        self.status.set("用（動作）、*動作* 或 /action 描述你想做的事。")

    @staticmethod
    def _action_speaker(action: UserAction) -> str:
        return {"mouse": "你 · 滑鼠互動", "menu": "你 · 選單互動", "text": "你 · 輸入動作"}.get(action.source, "你 · 互動")

    def _record_user_action(self, action: UserAction, *, input_text: str | None = None, **details):
        self._append("action", self._action_speaker(action), input_text if action.source == "text" and input_text else action.description)
        self._log("user_action", action_kind=action.kind, source=action.source, description=action.description,
                  input_text=input_text, **details)

    def _touch_reply_block_reason(self, now: float) -> str | None:
        if self.closing or not self.config.get("touch_dialogue", True):
            return "disabled"
        if now - self._last_touch_reply_at < 30:
            return "cooldown"
        if self._synthesis_pending or self._prewarm_pending or self.player.playing or self._audio_queue or (self.turn_id and not self._turn_complete):
            return "conversation_busy"
        if now - self._last_typing_at < 2 or self._chat_draft().strip():
            return "chat_input_active"
        snapshot = self.behavior.latest_snapshot
        if snapshot and (snapshot.desktop_available is not True or snapshot.foreground_fullscreen):
            return "desktop_unavailable"
        return None

    def _send_action(self, action: UserAction, *, input_text: str | None = None, touch=False, record=True) -> bool:
        if self.closing:
            return False
        now = time.monotonic()
        if touch and self._touch_reply_block_reason(now):
            return False
        text = format_user_action(action) if input_text is None else input_text
        self._start_turn(text, action=action, touch=touch, record=record)
        if touch:
            self._last_touch_reply_at = now
        return True

    def send_text(self, text: str) -> bool:
        text = text.strip()
        if not text or self.closing:
            return False
        if len(text) > 4000:
            self.status.set("這段有點長，請縮短至 4000 字以內。")
            return False
        action = parse_user_action(text)
        if action:
            return self._send_action(action, input_text=text)
        self._start_turn(text)
        return True

    def _start_turn(self, text: str, *, action: UserAction | None = None, touch=False, record=True):
        if not touch:
            self.stop(silent=True)
        self._turn_is_proactive = False
        self._turn_is_touch = touch
        self._heard = []
        self._reply = None
        self._voice_failed = False
        self._turn_complete = False
        self._turn_speak = self.speak_var.get()
        self._turn_profile = self.profile_var.get()
        self._current_input_text = text
        self._synthesis_pending = True
        self.turn_id = self.controller.start(text, speak=self._turn_speak, profile=self._turn_profile)
        if action and record:
            self._record_user_action(action, input_text=text if action.source == "text" else None)
        elif action is None:
            self._append("user", "你", text)
        self.status.set("二乃正在想…")
        if action:
            self._react_to_action(action)
        else:
            self._set_animation("thinking")
        self._log("turn_started", turn_id=self.turn_id, speak=self.speak_var.get(), profile=self._turn_profile,
                  interaction_source=action.source if action else "text", interaction_kind=action.kind if action else None,
                  touch_reply=touch)

    def retry_voice(self) -> bool:
        if self.closing or not self._last_reply:
            return False
        reply = self._last_reply
        self.stop(silent=True)
        self._turn_is_proactive = False
        self._heard = []
        self._reply = reply
        self._voice_failed = self._turn_complete = False
        self._turn_speak = True
        self._turn_profile = self._last_reply_profile
        self.profile_var.set(self._turn_profile)
        self._synthesis_pending = True
        self.health["voice"] = {key: value for key, value in self.health.get("voice", {}).items()
                                if key not in {"last_result", "resource_block", "resource_wait"}}
        self.turn_id = self.controller.retry_voice(reply, profile=self._turn_profile)
        self.replays.begin_retry(self.turn_id, reply["turn_id"], profile=self._turn_profile)
        self.status.set("正在重試上一則語音…")
        self._set_animation(EXPRESSION_ANIMATION.get(reply.get("expression"), "idle"))
        self.controller.record_delivery(self.turn_id, "", "interrupted")
        self._log("voice_retry_started", turn_id=self.turn_id, agent_turn_id=reply["turn_id"], profile=self._turn_profile)
        return True

    def stop(self, *, silent: bool = False):
        self._manual_interaction("stop", interrupt_touch=False)
        old = self.turn_id
        self.player.stop()
        self._audio_queue.clear()
        self._synthesis_pending = False
        self._prewarm_pending = False
        self._prewarm_id = None
        self.controller.stop()
        if old and self._reply and not self._turn_complete:
            self.controller.record_delivery(old, "".join(self._heard), "interrupted")
        self.turn_id = None
        self._turn_is_replay = False
        self._turn_is_touch = False
        self._touch_gesture = None
        self._touch_reaction_until = 0
        self._carry.reset()
        self._carry_sample = None
        self._drag = None
        self._dragged = False
        self._set_animation("idle")
        self._hide_bubble()
        if not silent:
            self.status.set("已停止。")
            self._log("stopped", turn_id=old, completed_segments=len(self._heard))

    def _append(self, kind: str, speaker: str, text: str, reply_ref: str | None = None):
        self._transcript_parts.append((kind, speaker, text, reply_ref) if reply_ref else (kind, speaker, text))
        # Visible history is bounded; the Agent owns persistent conversation state.
        self._transcript_parts = self._transcript_parts[-90:]
        self._render_transcript()

    def _render_transcript(self):
        if not self.transcript or not self.transcript.winfo_exists():
            return
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        for tag in self.transcript.tag_names():
            if tag.startswith(("replay_", "favorite_")):
                self.transcript.tag_delete(tag)
        for row, part in enumerate(self._transcript_parts):
            kind, speaker, text = part[:3]
            if kind == "subtitle" and not self.subtitle_var.get():
                continue
            if speaker:
                self.transcript.insert("end", speaker + "\n", "name")
            self.transcript.insert("end", text + "\n", kind)
            if kind == "reply" and len(part) > 3 and part[3]:
                record = self.replays.for_reply(part[3])
                available = bool(record and record["available_pieces"])
                replay_tag, favorite_tag = f"replay_{row}", f"favorite_{row}"
                self.transcript.tag_configure(replay_tag, foreground=ACCENT if available else MUTED, lmargin1=10)
                label = "▶ 重播" if record and record["complete"] else "▶ 重播片段" if available else "原音尚未保留"
                self.transcript.insert("end", label, replay_tag)
                self.transcript.insert("end", "    ")
                self.transcript.tag_configure(favorite_tag, foreground=ACCENT if available else MUTED)
                self.transcript.insert("end", "★ 已收藏" if record and record.get("favorite") else "☆ 收藏", favorite_tag)
                self.transcript.insert("end", "\n")
                if available:
                    self.transcript.tag_bind(replay_tag, "<Button-1>", lambda event, rid=record["id"]: self.replay_record(rid))
                    self.transcript.tag_bind(favorite_tag, "<Button-1>", lambda event, rid=record["id"]: self.toggle_favorite(rid))
                    for tag in (replay_tag, favorite_tag):
                        self.transcript.tag_bind(tag, "<Enter>", lambda event: self.transcript.configure(cursor="hand2"))
                        self.transcript.tag_bind(tag, "<Leave>", lambda event: self.transcript.configure(cursor="arrow"))
            self.transcript.insert("end", "\n")
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def _subtitles_changed(self):
        self._render_transcript()
        self._save()

    def replay_record(self, record_id: str) -> bool:
        if self.closing:
            return False
        record = self.replays.get(record_id)
        try:
            playlist = self.replays.playlist(record_id)
        except (OSError, ValueError) as exc:
            self.status.set(str(exc))
            self._refresh_voice_library()
            return False
        self.stop(silent=True)
        self._turn_is_proactive = self._turn_is_touch = False
        self._turn_is_replay = True
        self._active_proactive_decision = None
        self._reply = record["reply"]
        self._heard = []
        self._voice_failed = self._turn_complete = False
        self._turn_speak = True
        self._turn_profile = record["profile"]
        self._synthesis_pending = True
        self.turn_id = self.controller.replay_audio(record, playlist)
        self.status.set("正在重播原音…" if record["complete"] else "正在重播已保留的語音片段…")
        self._set_animation(EXPRESSION_ANIMATION.get(self._reply.get("expression"), "idle"))
        self._expression_until = time.monotonic() + record["seconds"] + 1
        self._log("replay_started", turn_id=self.turn_id, record_id=record_id,
                  agent_turn_id=record["agent_turn_id"], original_operation_id=record["operation_id"],
                  complete=record["complete"], wav_paths=[item["wav_path"] for item in playlist])
        return True

    def replay_reply(self, agent_turn_id: str) -> bool:
        """Resolve one actual reply occurrence, including imported old history."""
        record = self.replays.for_reply(agent_turn_id)
        if not record or not record["available_pieces"]:
            self.status.set("這則回覆沒有可重播的原音。")
            return False
        return self.replay_record(record["id"])

    def toggle_favorite(self, record_id: str):
        record = self.replays.get(record_id)
        if not record:
            return
        try:
            record = self.replays.set_favorite(record_id, not record.get("favorite"))
        except (OSError, ValueError) as exc:
            self.status.set(f"原音未能收藏：{exc}")
            return
        self.status.set("已收藏這次原音，之後仍可重播。" if record["favorite"] else "已取消收藏。")
        self._log("voice_favorite_changed", record_id=record_id, favorite=record["favorite"])
        self._render_transcript()
        self._refresh_voice_library()

    def open_voice_library(self):
        if self.closing:
            return
        if self.voice_library and self.voice_library.winfo_exists():
            self.voice_library.deiconify()
            self.voice_library.lift()
            self._refresh_voice_library()
            return
        window = self.voice_library = tk.Toplevel(self.root)
        window.title("二乃 · 語音紀錄／收藏")
        window.configure(bg=BG)
        window.attributes("-topmost", self.topmost_var.get())
        window.geometry("540x490")
        window.minsize(400, 360)
        window.protocol("WM_DELETE_WINDOW", window.withdraw)
        window.bind("<Escape>", lambda event: self.stop())
        window.columnconfigure(0, weight=1)
        window.rowconfigure(1, weight=1)
        header = ttk.Frame(window, style="Pet.TFrame", padding=(16, 14, 16, 8))
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="把喜歡的聲音留在這裡", style="Pet.TLabel",
                  font=("Microsoft JhengHei UI", 13, "bold")).pack(anchor="w")
        self._voice_favorites_only = tk.BooleanVar(window, False)
        ttk.Checkbutton(header, text="只看收藏", variable=self._voice_favorites_only,
                        command=self._refresh_voice_library, style="Pet.TCheckbutton").pack(anchor="w", pady=(6, 0))
        body = ttk.Frame(window, style="Pet.TFrame", padding=(16, 0, 16, 0))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self._voice_library_list = tk.Listbox(body, bg="white", fg=INK, relief="flat", exportselection=False,
                                             selectbackground=PALE, selectforeground=INK, activestyle="none")
        self._voice_library_list.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(body, command=self._voice_library_list.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self._voice_library_list.configure(yscrollcommand=scrollbar.set)
        self._voice_library_list.bind("<<ListboxSelect>>", lambda event: self._voice_library_selected())
        self._voice_library_list.bind("<Double-Button-1>", lambda event: self._replay_library_selection())
        footer = ttk.Frame(window, style="Pet.TFrame", padding=(16, 12, 16, 16))
        footer.grid(row=2, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        self._voice_library_detail = tk.StringVar(window)
        detail = ttk.Label(footer, textvariable=self._voice_library_detail, wraplength=490,
                           style="Muted.Pet.TLabel", justify="left")
        detail.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        window.bind("<Configure>", lambda event: detail.configure(wraplength=max(300, window.winfo_width() - 40)))
        self._voice_library_replay = ttk.Button(footer, text="▶ 重播", style="Accent.Pet.TButton", command=self._replay_library_selection)
        self._voice_library_replay.grid(row=1, column=0, sticky="w")
        self._voice_library_favorite = ttk.Button(footer, text="☆ 收藏", style="Pet.TButton", command=self._favorite_library_selection)
        self._voice_library_favorite.grid(row=1, column=1, padx=(6, 6))
        ttk.Button(footer, text="停止", style="Pet.TButton", command=self.stop).grid(row=1, column=2)
        self._refresh_voice_library()
        window.update_idletasks()
        if self.chat and self.chat.winfo_exists() and self.chat.state() != "withdrawn":
            chat_left, chat_top, chat_right, chat_bottom = window_rect(hwnd_of(self.chat))
            width, height = window.winfo_width(), window.winfo_height()
            work_left, work_top, work_right, work_bottom = work_area(chat_left + (chat_right - chat_left) // 2,
                                                                      chat_top + (chat_bottom - chat_top) // 2)
            x = chat_right + 12 if chat_right + 12 + width <= work_right else chat_left - width - 12
            x, y = clamp_position(x, chat_top, width, height)
            move_window(hwnd_of(window), x, y, width, height, topmost=self.topmost_var.get())

    def _selected_voice_id(self):
        selection = self._voice_library_list.curselection() if self._voice_library_list else ()
        return self._voice_library_ids[selection[0]] if selection and selection[0] < len(self._voice_library_ids) else None

    def _refresh_voice_library(self):
        if not getattr(self, "_voice_library_list", None) or not self._voice_library_list.winfo_exists():
            return
        selected = self._selected_voice_id()
        records = self.replays.records(favorites_only=self._voice_favorites_only.get())
        self._voice_library_ids = [record["id"] for record in records]
        self._voice_library_list.delete(0, "end")
        for record in records:
            stamp = time.strftime("%m/%d %H:%M", time.localtime(record["created_at"]))
            state = "" if record["complete"] else " · 僅片段" if record["available_pieces"] else " · 原音未保留"
            self._voice_library_list.insert("end", f"{'★' if record.get('favorite') else '☆'} {stamp}  {record['speech_text_ja'][:44]}{state}")
        if records:
            index = self._voice_library_ids.index(selected) if selected in self._voice_library_ids else 0
            self._voice_library_list.selection_set(index)
        self._voice_library_selected()

    def _voice_library_selected(self):
        record_id = self._selected_voice_id()
        record = self.replays.get(record_id) if record_id else None
        available = bool(record and record["available_pieces"])
        self._voice_library_replay.configure(state="normal" if available else "disabled",
                                             text="▶ 重播片段" if available and not record["complete"] else "▶ 重播")
        self._voice_library_favorite.configure(state="normal" if available or record and record.get("favorite") else "disabled",
                                               text="★ 取消收藏" if record and record.get("favorite") else "☆ 收藏")
        if not record:
            self._voice_library_detail.set("目前沒有收藏。點回覆旁的 ☆ 收藏，便能保留喜歡的原音。" if self._voice_favorites_only.get()
                                           else "語音完成後會出現在這裡。")
            return
        state = "完整原音" if record["complete"] else "僅保留部分片段" if available else "原音未保留，無法重播"
        text = f"{record['speech_text_ja']}\n{record.get('subtitle_zh', '')}\n{state} · {record['seconds']:.2f} 秒 · {record['available_pieces']} 段"
        self._voice_library_detail.set(text)

    def _replay_library_selection(self):
        record_id = self._selected_voice_id()
        if record_id:
            self.replay_record(record_id)

    def _favorite_library_selection(self):
        record_id = self._selected_voice_id()
        if record_id:
            self.toggle_favorite(record_id)

    def _posture_animation(self, name: str) -> str:
        # Whole-body standing expressions must not pop a seated pet upright.
        # Old packs retain their existing sit pose; text/audio still respond.
        if self.seated_var.get() and name not in {"sit", "sit_down", "sit_swing", "carried", "land"}:
            name = "sit"
        if name not in self.atlas.manifest.get("animations", {}):
            name = {"sit_down": "sit", "sit_swing": "sit", "stand_up": "idle",
                    "carried": "sit" if self.seated_var.get() else "dragged",
                    "land": "sit" if self.seated_var.get() else "idle",
                    "head_tilt": "curious", "crossed_arms": "annoyed"}.get(name, name)
        return self.atlas.resolve_animation(name)

    def _speaking_frame(self, now: float) -> str:
        mouth = self.player.mouth()
        if self.seated_var.get():
            seated_mouth = "sit_" + mouth
            if seated_mouth in self.atlas.manifest["frames"]:
                return seated_mouth
            return self.atlas.frame("sit", (now - self._animation_start) * 1000)
        return self.atlas.resolve_frame(mouth)

    def _set_animation(self, name: str):
        name = self._posture_animation(name)
        if self._animation != name:
            self._animation, self._animation_start = name, time.monotonic()

    def _input_focus(self, focused: bool):
        self._chat_input_focused = focused

    def _input_activity(self, event=None):
        self._last_typing_at = time.monotonic()
        self._manual_interaction("typing")

    def _cancel_touch_reply(self, reason: str):
        if not self._turn_is_touch:
            return
        old = self.turn_id
        self.controller.stop()
        self.player.stop()
        self._audio_queue.clear()
        self._synthesis_pending = False
        if old and self._reply:
            self.controller.record_delivery(old, "".join(self._heard), "interrupted" if self._heard else "discarded")
        self.turn_id = None
        self._turn_complete = True
        self._turn_is_touch = False
        self._reply = None
        self._hide_bubble()
        self._log("touch_reply_cancelled", turn_id=old, reason=reason)

    def _manual_interaction(self, reason: str, *, interrupt_touch=True):
        self._last_manual_at = time.monotonic()
        self._reply_action_until = 0
        if getattr(self, "_turn_is_replay", False) and reason in {"typing", "input_focus"}:
            self.stop(silent=True)
            self.status.set("已停止重播。")
        if not hasattr(self, "behavior"):
            return
        self.behavior.note_manual_interaction(self._last_manual_at)
        self._hide_bubble()
        if interrupt_touch:
            self._cancel_touch_reply(reason)
        if getattr(self, "_turn_is_proactive", False):
            old = self.turn_id
            self.controller.cancel_proactive(reason)
            self.player.stop()
            self._audio_queue.clear()
            self._synthesis_pending = False
            if old:
                self.controller.record_delivery(old, "".join(self._heard), "interrupted" if self._heard else "discarded")
            self.turn_id = None
            self._turn_complete = True
            self._turn_is_proactive = False
            self._active_proactive_decision = None
            self._hide_bubble()
            self.status.set("想聊什麼都可以。")

    def _presentation_interaction(self, reason: str):
        # Moving or restyling the pet is not a new conversational turn. Keep
        # already generated nonvisual speech eligible while delaying new topics.
        decision = getattr(self, "_active_proactive_decision", None)
        if (self._turn_is_proactive and decision is not None and
                decision.event.kind == ProactiveEventKind.SCREEN_OBSERVATION):
            self._manual_interaction(reason)
        elif hasattr(self, "behavior"):
            self.behavior.note_presentation_interaction(time.monotonic())

    def _busy_state(self, now: float, *, delivery=False) -> BusyState:
        snapshot = self.behavior.latest_snapshot
        voice = self.health.get("voice", {})
        issue = resource_issue(voice)
        waiting = bool(issue and issue["waiting"])
        own = delivery and self._turn_is_proactive
        return BusyState(speaking=False if own else self.player.playing or bool(self._audio_queue),
                         typing=now - self._last_typing_at < 2,
                         input_active=bool(self._chat_draft().strip()),
                         manual_chat=now - self._last_manual_at < 3,
                         dragging=self._drag is not None and self._dragged,
                         interacting=self._touch_visual_active(now) and not
                                     (own and getattr(self, "_touch_is_presentation", False)),
                         chat_active=bool(self.chat and self.chat.winfo_exists() and self.chat.state() != "withdrawn"),
                         thinking=False if own else self._synthesis_pending and self._reply is None,
                         synthesizing=False if own else (self._prewarm_pending or self._synthesis_pending and self._reply is not None) and not waiting,
                         fullscreen=bool(snapshot and snapshot.foreground_fullscreen),
                         locked=bool(snapshot and snapshot.desktop_available is False),
                         hidden=self.root.state() == "withdrawn",
                         desktop_available=snapshot.desktop_available if snapshot else None,
                         resource_waiting=waiting,
                         voice_unavailable=(not self.speak_var.get() or voice.get("voice_state") == "unloaded_for_resources" or
                                            voice.get("status") == "unavailable" or voice.get("backend_stage") in {"worker_exited", "blocked_memory"}))

    def _proactive_guard(self, decision, *, audio_prepared=False, continuing=False):
        if decision.event.source == ObservationSource.INJECTED and not self.config.get("allow_injected_proactive", False):
            return False
        return self.behavior.should_deliver(decision, audio_prepared=audio_prepared, continuing=continuing)

    def _behavior_tick(self, now: float):
        busy = self._busy_state(now)
        snapshot = self.behavior.observe(now, self._busy_state(now, delivery=True))
        self.behavior.delivery_busy = self._busy_state(now, delivery=True)
        if snapshot is not None:
            signature = (snapshot.source, snapshot.category, snapshot.foreground_fullscreen,
                         snapshot.desktop_available, int((snapshot.idle_seconds or 0) // 30))
            if signature != self._activity_signature:
                self._activity_signature = signature
                self._log("activity_sample", source=snapshot.source.value, category=snapshot.category.value,
                          idle_seconds=snapshot.idle_seconds, fullscreen=snapshot.foreground_fullscreen,
                          desktop_available=snapshot.desktop_available, foreground_owned=snapshot.foreground_owned)
            proactive_status = self.behavior.proactive.status(now)
            reason = proactive_status["suppression_reason"]
            if reason != self._proactive_status_signature and now - self._last_proactive_status_log_at >= 30:
                self._proactive_status_signature = reason
                self._last_proactive_status_log_at = now
                self._log("proactive_status", **proactive_status)
        continuing = bool(self.player.item and self.player.item["turn_id"] == self.turn_id)
        if self._turn_is_proactive and self.turn_id and not self.controller.allow_delivery(self.turn_id, continuing=continuing):
            self.controller.cancel_proactive("eligibility_changed")
        x, y, _, _ = window_rect(self.native.hwnd)
        # The full topology governs movement. room is only the compatibility
        # fallback argument; it is not a per-monitor clamp during a portal leg.
        area = self.behavior.topology.areas[0] if self.behavior.topology and self.behavior.topology.areas else None
        room = (area.left, area.top, area.right, area.bottom) if area else work_area(x, y, self.width, self.height)
        motion_busy = replace(busy, interacting=True) if now < self._reply_action_until else busy
        if self._drag is not None and not self._dragged:
            motion_busy = replace(motion_busy, interacting=True)
        motion = self.behavior.step(now, position=(x, y), room=room, mouse=cursor_position(), busy=motion_busy,
                                    animations=self.atlas.available_behavior_animations(),
                                    allow_idle_actions=now >= self._expression_until)
        if motion.moved:
            move_window(self.native.hwnd, round(motion.position[0]), round(motion.position[1]), self.width, self.height)
        previous_motion_state = self._last_motion_state
        if motion.state != self._last_motion_state:
            self._last_motion_state = motion.state
            self._log("motion_state", state=motion.state.value, animation=motion.animation,
                      reason=motion.reason, fallback=motion.animation_fallback)
        walking_states = {MotionState.WALKING_LEFT, MotionState.WALKING_RIGHT, MotionState.WALKING_UP, MotionState.WALKING_DOWN}
        walk_step = motion.moved and motion.state in walking_states
        walk_stopped = previous_motion_state in walking_states and motion.state not in walking_states
        if motion.state == MotionState.DRAGGED or motion.reason == "posture_transition":
            self._set_animation(motion.animation)
        elif self._touch_visual_active(now):
            self._set_animation(self._touch_animation)
        elif motion.state == MotionState.SEATED:
            self._set_animation(motion.animation)
        elif not busy.action_block_reason and (motion.moved or now >= self._expression_until):
            self._set_animation(motion.animation)
        elif busy.action_block_reason and previous_motion_state in walking_states and not self.player.playing:
            self._set_animation(motion.animation)

    def _begin_proactive(self, decision):
        if decision.event.source == ObservationSource.INJECTED and not self.config.get("allow_injected_proactive", False):
            return
        if not self.behavior.accepts_action(decision):
            return
        self.behavior.idle_actions.cancel(time.monotonic())
        self._set_animation(decision.animation)
        self._expression_until = time.monotonic() + 4
        if not decision.request_speech:
            return
        if not self._proactive_guard(decision):
            return
        operation = self.controller.start_proactive(decision, should_deliver=self._proactive_guard,
                                                     speak=self.speak_var.get(), profile=self.profile_var.get())
        if not operation:
            return
        self.turn_id = operation
        self._turn_is_proactive = True
        self._active_proactive_decision = decision
        self._reply = None
        self._heard = []
        self._audio_queue.clear()
        self._voice_failed = self._turn_complete = False
        self._turn_speak = self.speak_var.get()
        self._turn_profile = self.profile_var.get()
        self._synthesis_pending = True
        self._log("proactive_started", turn_id=operation, event_kind=decision.event.kind.value,
                  source=decision.event.source.value, expires_at=decision.expires_at)

    def simulate_proactive_return(self):
        if not self.config.get("allow_injected_proactive", False):
            raise ValueError("Injected proactive events require an explicit integration config.")
        if self._synthesis_pending or self.player.playing or self._audio_queue:
            return None
        self._chat_input_focused = False
        self._last_manual_at = self._last_typing_at = float("-inf")
        self.behavior.delivery_busy = replace(self._busy_state(time.monotonic()), input_active=False, manual_chat=False)
        return self.behavior.simulate_return()

    def _show_bubble(self, reply: dict):
        self._hide_bubble()
        self._bubble_turn_id = self.turn_id
        window = self.bubble = tk.Toplevel(self.root)
        window.withdraw()
        window.overrideredirect(True)
        window.configure(bg="#D9C6E8")
        window.attributes("-topmost", self.topmost_var.get())
        frame = tk.Frame(window, bg=BG, padx=14, pady=10)
        frame.pack(padx=1, pady=1)
        simulated = bool(self._active_proactive_decision and self._active_proactive_decision.event.source == ObservationSource.INJECTED)
        title = "二乃 · 回應互動" if self._turn_is_touch else "二乃主動 · 模擬事件" if simulated else "二乃主動"
        tk.Label(frame, text=title, bg=BG, fg=ACCENT,
                 font=("Microsoft JhengHei UI", 9, "bold")).pack(anchor="w")
        text = reply["speech_text_ja"]
        if self.subtitle_var.get() and reply.get("subtitle_zh"):
            text += "\n" + reply["subtitle_zh"]
        tk.Label(frame, text=text, bg=BG, fg=INK, wraplength=260, justify="left").pack(anchor="w", pady=(4, 0))
        window.bind("<Button-1>", lambda event: self.open_chat())
        window.update_idletasks()
        hwnd = passive_window(window)
        width, height = window.winfo_reqwidth(), window.winfo_reqheight()
        x, y, right, bottom = window_rect(self.native.hwnd)
        x, y = clamp_position(x + (self.width - width) // 2, y - height - 10, width, height)
        window.deiconify()
        move_window(hwnd, x, y, width, height, topmost=self.topmost_var.get())
        token = self._bubble_turn_id
        self.root.after(12000, lambda: self._hide_bubble() if self._bubble_turn_id == token else None)

    def _hide_bubble(self):
        if getattr(self, "bubble", None) is not None and self.bubble.winfo_exists():
            self.bubble.destroy()
        self.bubble = None
        self._bubble_turn_id = None

    def _handle_event(self, event: dict):
        kind = event["type"]
        if kind == "proactive_draining":
            self._log("proactive_draining", turn_id=event.get("turn_id"), reason=event.get("reason"))
            if event.get("turn_id") == self.turn_id and self._turn_is_proactive:
                self._audio_queue.clear()
                self._synthesis_pending = False
                self._voice_failed = True
            return
        if kind == "proactive_decision":
            decision = event["decision"]
            self._log("proactive_decision", event_kind=decision.event.kind.value, source=decision.event.source.value,
                      request_speech=decision.request_speech, reason=decision.reason)
            self._begin_proactive(decision)
            return
        if kind == "proactive_discarded":
            self._log("proactive_discarded", turn_id=event.get("turn_id"), reason=event.get("reason"))
            if event.get("turn_id") == self.turn_id and self._turn_is_proactive:
                self.player.stop()
                self._audio_queue.clear()
                self._synthesis_pending = False
                self.controller.record_delivery(self.turn_id, "".join(self._heard), "interrupted" if self._heard else "discarded")
                self.turn_id = None
                self._reply = None
                self._turn_is_proactive = False
                self._turn_complete = True
                self._active_proactive_decision = None
                self._hide_bubble()
                self._set_animation("idle")
            return
        if kind == "native":
            action = event["action"]
            if action == "menu":
                self.show_menu(*event["position"])
            elif action == "restore_tray":
                self.native.restore_tray()
            elif action == "recover":
                self.recover()
            else:
                self.open_chat()
            return
        if kind == "health":
            if event["service"] == "voice" and self.turn_id and not self._prewarm_pending and not getattr(self, "_turn_is_replay", False):
                active = event["value"].get("active_turn_id")
                if active and active != self.turn_id:
                    # A queued health sample from the cancelled operation must
                    # not overwrite the retry's state or resource explanation.
                    self._log("stale_voice_health_discarded", turn_id=active)
                    return
            self.health[event["service"]] = event["value"]
            if event["service"] == "voice" and (self._prewarm_pending or self._synthesis_pending and self._reply) and not self.player.playing and not getattr(self, "_turn_is_replay", False):
                stage = event["value"].get("backend_stage", "not_loaded")
                issue = resource_issue(event["value"])
                if issue and issue["waiting"]:
                    self.status.set(resource_status(event["value"]))
                elif not event["value"].get("resident") and stage not in ("ready", "not_loaded", "blocked_memory", "worker_exited"):
                    self.status.set("第一次載入語音，稍等一下…桌寵仍可操作。")
            value = event["value"]
            signature = (value.get("status"), value.get("backend_stage"), value.get("voice_state"),
                         value.get("active_turn_id"), value.get("active_request_id"),
                         int(((value.get("resource_wait") or {}).get("elapsed_seconds") or 0) // 2))
            if self._health_log_signatures.get(event["service"]) != signature:
                self._health_log_signatures[event["service"]] = signature
                self._log("health", service=event["service"], value=value)
            self._refresh_status_panel()
            return
        if kind == "prewarm":
            if event.get("prewarm_id") != self._prewarm_id:
                self._log("stale_prewarm_event_discarded", prewarm_id=event.get("prewarm_id"))
                return
            self._prewarm_pending = event["status"] == "loading"
            if event.get("result"):
                self.health["voice"] = {**self.health.get("voice", {}), "last_result": event["result"]}
            self.status.set("正在載入本機語音…" if self._prewarm_pending else
                            "語音已載入；第一句仍可能需要等候。" if event["status"] == "ready" else
                            resource_status(event.get("result", {})) or "語音尚未準備完成，請查看設定中的狀態。")
            self._log("prewarm", value=event)
            self._refresh_status_panel()
            return
        if kind in ("diagnostic", "stale_discarded", "cancel_requested"):
            self._log(kind, **{key: value for key, value in event.items() if key != "type"})
            if event.get("error"):
                self.diagnostics.append(event["error"])
            return
        if event.get("turn_id") != self.turn_id or not self.controller.current(event.get("turn_id", "")):
            self._log("stale_ui_event_discarded", event_kind=kind, turn_id=event.get("turn_id"))
            return
        if event.get("proactive") and not self.controller.allow_delivery(self.turn_id):
            self.controller.cancel_proactive("expired_or_busy_ui_event")
            return
        if kind == "phase":
            if not self.player.playing:
                self.status.set({"thinking": "二乃正在想…", "observing": "二乃看看目前畫面…"}.get(event["value"], "正在準備二乃的聲音…"))
        elif kind == "screen_observation":
            self._log("screen_observation", turn_id=self.turn_id, observation=event["observation"])
        elif kind == "reply":
            self._reply = reply = event["reply"]
            self._turn_is_replay = False
            try:
                self.replays.add_reply(reply, self.turn_id, profile=self._turn_profile)
            except OSError as exc:
                self._log("voice_history_error", error=str(exc))
            proactive = event.get("proactive", False)
            if not proactive and reply["provider_mode"] in {"codex_subscription", "local_memory"}:
                self._last_reply = dict(reply)
                self._last_reply_profile = self._turn_profile
                try:
                    store_last_reply(self.state_dir / "last-reply.json", reply,
                                     input_text=self._current_input_text, profile=self._turn_profile)
                except OSError as exc:
                    self._log("reply_restore_cache_error", error=str(exc))
                self._update_retry_actions()
            name = "二乃 · 離線固定台詞示範" if reply["provider_mode"] == "offline_demo" else "二乃"
            if proactive:
                simulated = self._active_proactive_decision.event.source == ObservationSource.INJECTED
                name = "二乃主動 · 模擬事件" if simulated else "二乃主動"
                self._show_bubble(reply)
            elif self._turn_is_touch:
                self._show_bubble(reply)
            self._append("reply", name, reply["speech_text_ja"], reply["turn_id"])
            if reply.get("subtitle_zh"):
                self._append("subtitle", "", reply["subtitle_zh"])
            self._reply_animation(reply, time.monotonic())
            self._expression_until = time.monotonic() + 6
            self._motion = reply.get("motion", "idle")
            self._motion_start = time.monotonic()
            self.controller.record_delivery(self.turn_id, "", "interrupted")
            self._log("proactive_reply" if proactive else "reply", turn_id=self.turn_id, reply=reply, profile=self._turn_profile)
        elif kind == "voice_result":
            self._log("voice_result", turn_id=self.turn_id, agent_turn_id=event.get("agent_turn_id"), result=event["result"])
            try:
                self.replays.add_result(self.turn_id, event["result"])
                self._render_transcript()
                self._refresh_voice_library()
            except OSError as exc:
                self._log("voice_history_error", error=str(exc))
            self.health["voice"] = {**self.health.get("voice", {}), "last_result": event["result"]}
            if (event["result"].get("status") == "generated" and
                    self.health["voice"].get("voice_state") != "unloaded_for_resources" and
                    self.health["voice"].get("backend_stage") != "unloaded_for_resources"):
                self.health["voice"].pop("resource_wait", None)
                self.health["voice"].pop("resource_block", None)
                self.health["voice"].update(resident=True, backend_stage="ready")
            self._refresh_status_panel()
        elif kind == "audio":
            accepted = event["result"].get("status") == ("replay" if event.get("replay") else "generated")
            if accepted and event["result"].get("playable"):
                self._audio_queue.append(event)
        elif kind == "voice_error":
            self._voice_failed = True
            result = event["result"]
            self.health["voice"] = {**self.health.get("voice", {}), "last_result": result}
            self.status.set(resource_status(result) or "這次語音未能完成，可按「重試語音」。")
            self._refresh_status_panel()
        elif kind == "finished":
            self._synthesis_pending = False
            if not event.get("replay"):
                try:
                    self.replays.finish(self.turn_id)
                    self._render_transcript()
                    self._refresh_voice_library()
                except OSError as exc:
                    self._log("voice_history_error", error=str(exc))
            if not event.get("voice"):
                self.status.set("二乃回覆了。")
            self._finish_if_ready()
        elif kind == "error":
            self._synthesis_pending = False
            self._voice_failed = True
            self.diagnostics.append(event["error"])
            self.status.set("這次語音未能完成，可按「重試語音」。" if self._reply else
                            "這次沒有接上對話，請從設定查看連線狀態。")
            self._set_animation("idle")
            if self._turn_is_touch:
                self._turn_is_touch = False
                self._turn_complete = True
            self._log("turn_error", turn_id=self.turn_id, error=event["error"])
            self._refresh_status_panel()

    def _finish_if_ready(self):
        if self._turn_complete or self._synthesis_pending or self._audio_queue or self.player.item is not None:
            return
        self._turn_complete = True
        self.controller.complete_delivery(self.turn_id)
        if not self._voice_failed:
            self.status.set("原音重播完畢。" if getattr(self, "_turn_is_replay", False) else "二乃在這裡，想聊什麼都可以。")
        if self._reply and self.turn_id:
            if not self._turn_speak:
                self.controller.record_delivery(self.turn_id, self._reply["speech_text_ja"])
            else:
                self.controller.record_delivery(self.turn_id, "".join(self._heard),
                                                "interrupted" if self._voice_failed else "delivered")
        self._log("replay_finished" if getattr(self, "_turn_is_replay", False) else "turn_finished", turn_id=self.turn_id, voice_failed=self._voice_failed,
                  completed_segments=len(self._heard))
        if self._turn_is_proactive or self._turn_is_touch:
            self.status.set("二乃在這裡。")
            token = self._bubble_turn_id
            self.root.after(1400, lambda: self._hide_bubble() if self._bubble_turn_id == token else None)
            self._turn_is_proactive = False
            self._turn_is_touch = False
            self._active_proactive_decision = None
        if self.config.get("exit_when_done") and self.turn_id:
            self.root.after(350, self.close)

    def _tick(self):
        if self.closing:
            return
        for _ in range(100):
            try:
                self._handle_event(self.events.get_nowait())
            except queue.Empty:
                break
        self._behavior_tick(time.monotonic())
        if self.player.item is not None and not self.player.playing:
            item = self.player.item
            self.controller.mark_playback(item["turn_id"], False)
            if item["turn_id"] == self.turn_id:
                self._heard.append(item["text"])
                self.controller.record_delivery(self.turn_id, "".join(self._heard), "interrupted")
                self._log("playback_finished", turn_id=self.turn_id, request_id=item["result"].get("request_id"))
            self.player.stop()
            self._finish_if_ready()
        if self.player.item is None and self._audio_queue:
            item = self._audio_queue.popleft()
            if item["turn_id"] == self.turn_id and self.controller.allow_delivery(self.turn_id):
                try:
                    self.controller.mark_playback(self.turn_id, True)
                    self.player.play(item)
                    self.status.set("正在重播原音…" if item.get("replay") else "二乃在說話…")
                    self._log("playback_started", turn_id=self.turn_id, request_id=item["result"].get("request_id"),
                              wav_path=item["result"]["wav_path"])
                except Exception as exc:
                    self.controller.mark_playback(self.turn_id, False)
                    self._voice_failed = True
                    self.diagnostics.append(str(exc))
                    self.status.set("已回覆；音訊播放未能完成。")
                    self._log("playback_error", error=str(exc))
                    self._finish_if_ready()
        now = time.monotonic()
        if self._reply_action_until and now >= self._reply_action_until:
            self._reply_action_until = 0
            if self._reply and now < self._expression_until and not self._touch_visual_active(now):
                self._set_animation(EXPRESSION_ANIMATION.get(self._reply.get("expression"), "idle"))
        if self._touch_reaction_until and not self._touch_gesture and now >= self._touch_reaction_until:
            self._touch_reaction_until = 0
            # A movement selected by this tick already owns the animation.
            # Restoring the face must not replace its first walking frame.
            if self._animation == self._touch_animation:
                if self._synthesis_pending and self._reply is None:
                    self._set_animation("thinking")
                elif self._reply and now < self._expression_until:
                    self._set_animation(EXPRESSION_ANIMATION.get(self._reply.get("expression"), "idle"))
                else:
                    self._set_animation("idle")
        self._carry_sample = self._carry.sample(now)
        sprite_image = None
        if self._carry_sample is not None:
            carried = self._carry_sample
            carried_frame = carried.frame
            if carried.phase == "landing" and self.seated_var.get():
                posture = self._posture_animation("sit_down")
                carried_frame = self.atlas.frame(posture, carried.progress * self.atlas.animation_duration(posture) * 1000)
            frame, sprite_image = self.atlas.carry_image(carried_frame, carried.angle_degrees, self._carry_grip)
        elif self._drag is not None and self._dragged:
            # An aborted/reconfigured physics state must never revive the old
            # elapsed-time pose loop while the pointer is still held.
            frame, sprite_image = self.atlas.carry_image("carried_4", 0, self._carry_grip)
        elif self._touch_visual_active(now):
            frame = self.atlas.frame(self._posture_animation(self._touch_animation), (now - self._animation_start) * 1000)
        elif self.player.playing:
            frame = self._speaking_frame(now)
        else:
            if self._expression_until and now > self._expression_until and not self._synthesis_pending:
                self._set_animation("idle")
                self._expression_until = 0
            frame = self.atlas.frame(self._animation, (now - self._animation_start) * 1000)
        if frame != self._last_frame:
            self.canvas.itemconfigure(self.sprite_item, image=sprite_image if sprite_image is not None else self.atlas.images[frame])
            self._last_frame = frame
        elapsed = now - self._motion_start
        dx = dy = 0
        if self._carry_sample is not None:
            dy = round(self._carry_sample.offset_y * float(self.atlas.scale))
        elif (elapsed < 0.8 and not self._touch_visual_active(now) and not self.seated_var.get()
              and not (self._drag is not None and self._dragged)):
            wave = math.sin(elapsed * math.pi * 5)
            if self._motion == "nod":
                dy = round(abs(wave) * 3)
            elif self._motion == "bounce":
                dy = -round(abs(wave) * 7)
            elif self._motion == "shake":
                dx = round(wave * 4)
        self.canvas.coords(self.sprite_item, 12 + dx, 10 + dy)
        self.root.after(35, self._tick)

    def open_settings(self):
        if self.settings and self.settings.winfo_exists():
            self.settings.deiconify()
            self.settings.lift()
            self._refresh_status_panel()
            return
        window = self.settings = tk.Toplevel(self.root)
        window.withdraw()
        window.title("二乃 · 設定與狀態")
        window.configure(bg=BG)
        window.attributes("-topmost", self.topmost_var.get())
        window.resizable(False, False)
        window.protocol("WM_DELETE_WINDOW", window.withdraw)
        frame = ttk.Frame(window, style="Pet.TFrame", padding=22)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="陪伴方式", font=("Microsoft JhengHei UI", 14, "bold"), style="Pet.TLabel").pack(anchor="w", pady=(0, 12))
        for text, variable, command in (
            ("二乃保持在其他視窗上方", self.topmost_var, self._topmost_changed),
            ("播放日文語音", self.speak_var, self._voice_changed),
            ("顯示中文字幕", self.subtitle_var, self._subtitles_changed),
            ("滑鼠穿透（托盤或 Ctrl＋Alt＋N 可恢復）", self.clickthrough_var, self.set_click_through),
            ("自行走動", self.roaming_var, self._behavior_options_changed),
        ):
            ttk.Checkbutton(frame, text=text, variable=variable, command=command, style="Pet.TCheckbutton").pack(anchor="w")
        skin_row = ttk.Frame(frame, style="Pet.TFrame")
        skin_row.pack(fill="x", pady=(8, 0))
        ttk.Label(skin_row, text="外觀  ", style="Pet.TLabel").pack(side="left")
        for label, value in (("像素 v3", "pixel_v3"), ("像素 v2", "pixel_v2"), ("Q 版 v1", "q_v1")):
            if value not in self._skin_paths:
                continue
            ttk.Radiobutton(skin_row, text=label, value=value, variable=self.skin_var,
                            command=self._skin_changed, style="Pet.TRadiobutton").pack(side="left", padx=4)
        pace_row = ttk.Frame(frame, style="Pet.TFrame")
        pace_row.pack(fill="x", pady=(6, 0))
        ttk.Label(pace_row, text="主動頻率  ", style="Pet.TLabel").pack(side="left")
        for label, value in (("安靜", "quiet"), ("偶爾", "occasional"), ("活潑", "lively")):
            ttk.Radiobutton(pace_row, text=label, value=value, variable=self.proactive_var,
                            command=self._behavior_options_changed, style="Pet.TRadiobutton").pack(side="left", padx=3)
        ttk.Button(pace_row, text="看畫面與小動作…", style="Action.Pet.TButton",
                   command=self.open_companion_settings).pack(side="right", padx=(8, 0))
        size_row = ttk.Frame(frame, style="Pet.TFrame")
        size_row.pack(fill="x", pady=(10, 14))
        ttk.Label(size_row, text="角色大小  ", style="Pet.TLabel").pack(side="left")
        for label, value in (("小", "small"), ("中", "medium"), ("大", "large")):
            ttk.Radiobutton(size_row, text=label, value=value, variable=self.size_var,
                            command=self._size_changed, style="Pet.TRadiobutton").pack(side="left", padx=4)
        profile_row = ttk.Frame(frame, style="Pet.TFrame")
        profile_row.pack(fill="x", pady=(0, 8))
        ttk.Label(profile_row, text="語音速度  ", style="Pet.TLabel").pack(side="left")
        for label, value in (("品質優先", "quality_40"), ("互動速度（實驗）", "interactive_6_sway")):
            ttk.Radiobutton(profile_row, text=label, value=value, variable=self.profile_var,
                            command=self._profile_changed, style="Pet.TRadiobutton").pack(side="left", padx=2)
        voice_actions = ttk.Frame(frame, style="Pet.TFrame")
        voice_actions.pack(fill="x", pady=(0, 10))
        ttk.Button(voice_actions, text="準備語音", style="Pet.TButton", command=self.prepare_voice).pack(side="left")
        retry = ttk.Button(voice_actions, text="重試語音", style="Pet.TButton", command=self.retry_voice)
        retry.pack(side="left", padx=8)
        self._retry_buttons.append(retry)
        self._update_retry_actions()
        self.status_panel = tk.Text(frame, width=49, height=12, bg="white", fg=INK, wrap="word",
                                    relief="flat", padx=12, pady=12, state="disabled")
        self.status_panel.pack(fill="x", pady=(0, 12))
        actions = ttk.Frame(frame, style="Pet.TFrame")
        actions.pack(fill="x")
        ttk.Button(actions, text="更新狀態", style="Pet.TButton", command=self.controller.refresh_health).pack(side="left")
        ttk.Button(actions, text="恢復操作", style="Pet.TButton", command=self.recover).pack(side="left", padx=8)
        ttk.Button(actions, text="退出", style="Pet.TButton", command=self.close).pack(side="right")
        self._refresh_status_panel()
        window.update_idletasks()
        reference = self.chat if self.chat and self.chat.state() != "withdrawn" else self.root
        reference_rect = window_rect(hwnd_of(reference))
        window.deiconify()
        window.update_idletasks()
        current = window_rect(hwnd_of(window))
        width, height = current[2] - current[0], current[3] - current[1]
        x, y = clamp_position(reference_rect[0] + 18, reference_rect[1] + 18, width, height)
        move_window(hwnd_of(window), x, y, width, height, topmost=self.topmost_var.get())
        window.lift()

    def open_companion_settings(self):
        if self.companion_settings and self.companion_settings.winfo_exists():
            self.companion_settings.deiconify()
            self.companion_settings.lift()
            return
        window = self.companion_settings = tk.Toplevel(self.root)
        window.withdraw()
        window.title("二乃 · 看畫面與小動作")
        window.configure(bg=BG)
        window.attributes("-topmost", self.topmost_var.get())
        window.resizable(False, False)
        window.protocol("WM_DELETE_WINDOW", window.withdraw)
        frame = ttk.Frame(window, style="Pet.TFrame", padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Checkbutton(frame, text="偶爾看看目前畫面，再主動聊一兩句", variable=self.screen_enabled_var,
                        command=self._behavior_options_changed, style="Pet.TCheckbutton").pack(anchor="w")
        ttk.Label(frame, text="會將當次的一張畫面交給 Codex 讀圖。安靜模式暫停看畫面。",
                  style="Muted.Pet.TLabel", wraplength=405).pack(anchor="w", pady=(0, 8))
        for title, variable, choices in (
            ("看哪裡", self.screen_scope_var, (("目前視窗", "active_window"), ("目前視窗所在螢幕", "active_screen"))),
            ("看畫面頻率", self.screen_frequency_var, (("較少", "less"), ("偶爾 · 5–12 分鐘", "normal"), ("較多", "more"))),
            ("輸入閒置多久後再互動", self.return_idle_var, (("3 分鐘", 180), ("5 分鐘", 300))),
        ):
            row = ttk.Frame(frame, style="Pet.TFrame")
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=title + "  ", style="Pet.TLabel").pack(side="left")
            for label, value in choices:
                ttk.Radiobutton(row, text=label, value=value, variable=variable,
                                command=self._behavior_options_changed, style="Pet.TRadiobutton").pack(side="left", padx=2)
        ttk.Checkbutton(frame, text="待機時做揮手、伸懶腰等小動作", variable=self.idle_actions_var,
                        command=self._behavior_options_changed, style="Pet.TCheckbutton").pack(anchor="w", pady=(8, 0))
        ttk.Label(frame, text="主動互動共用冷卻與每小時上限；輸入、拖曳和說話時會暫停。",
                  style="Muted.Pet.TLabel", wraplength=405).pack(anchor="w", pady=(4, 10))
        ttk.Button(frame, text="完成", style="Pet.TButton", command=window.withdraw).pack(anchor="e")
        window.update_idletasks()
        reference = self.settings if self.settings and self.settings.winfo_exists() else self.root
        rect = window_rect(hwnd_of(reference))
        window.deiconify()
        window.update_idletasks()
        current = window_rect(hwnd_of(window))
        width, height = current[2] - current[0], current[3] - current[1]
        x, y = clamp_position(rect[0] + 20, rect[1] + 20, width, height)
        move_window(hwnd_of(window), x, y, width, height, topmost=self.topmost_var.get())
        window.lift()

    def _refresh_status_panel(self):
        if not self.settings or not self.settings.winfo_exists() or not hasattr(self, "status_panel"):
            return
        agent, voice = self.health.get("agent", {}), self.health.get("voice", {})
        if agent.get("provider_mode") == "codex_subscription":
            dialogue = "Codex 已登入" if agent.get("llm_ready") is not False else "Codex 連線尚未就緒"
        elif agent.get("provider_mode") == "offline_demo":
            dialogue = "離線固定台詞示範（非即時對話）"
        else:
            dialogue = "正在確認 Codex 狀態" if not agent else "Codex 尚未就緒"
        last = voice.get("last_result", {})
        issue = resource_issue(voice)
        if voice.get("voice_state") == "unloaded_for_resources" or voice.get("backend_stage") == "unloaded_for_resources":
            sound = "已因資源壓力卸載；可重試語音"
        elif voice.get("voice_state") == "reclaiming_cache":
            sound = "正在回收閒置快取"
        elif issue:
            sound = issue["headline"]
        elif voice.get("resident"):
            sound = "已就緒，模型常駐本機"
        elif voice.get("status") == "unavailable":
            sound = "尚未就緒"
        else:
            sound = "首次說話時載入本機模型"
        text = (f"對話：{dialogue}\n語音：{sound}\n"
                f"模式：{'互動速度（實驗，品質待確認）' if self.profile_var.get() == 'interactive_6_sway' else '品質優先'}\n")
        resources = resource_details(voice)
        if resources:
            text += "\n" + resources + "\n"
        text += (f"\n托盤：{'可用' if self.native.tray_available else '未建立'}\n"
                 f"恢復快捷鍵：{'Ctrl＋Alt＋N' if self.native.hotkey_available else '未註冊，請使用托盤'}\n"
                 "嘴形依 WAV 音量切換；句間可能有等待。\n")
        text += (f"看畫面：{'開啟' if self.screen_enabled_var.get() else '關閉'}，"
                 f"{'目前視窗' if self.screen_scope_var.get() == 'active_window' else '目前螢幕'}；"
                 "可從「看畫面與小動作」調整。\n")
        reasons = {"input_active": "正在編寫草稿", "typing": "正在輸入", "manual_chat": "剛操作聊天",
                   "manual_cooldown": "剛互動，稍後再聊", "presentation_cooldown": "剛調整外觀，稍後再聊",
                   "speaking": "正在說話", "thinking": "正在想回覆",
                   "synthesizing": "正在準備語音", "dragging": "正在拖曳", "interacting": "正在互動",
                   "fullscreen": "目前是全螢幕", "desktop_unavailable": "桌面暫不可用", "hidden": "角色已隱藏",
                   "resource_waiting": "等待語音資源", "voice_unavailable": "語音暫不可用",
                   "own_foreground": "目前前景是二乃，略過看畫面", "user_idle": "等你操作電腦時再聊",
                   "global_cooldown": "讓話題留點空間", "event_cooldown": "稍後換個話題", "hourly_budget": "這一小時先安靜陪伴",
                   "quiet_mode": "安靜陪伴", "awaiting_event": "等適合的時機", "no_observation": "等待桌面狀態"}
        proactive = self.behavior.proactive.status()
        reason = proactive["suppression_reason"]
        text += f"陪伴姿勢：{'坐在原地' if self.seated_var.get() else '自由活動'}\n"
        text += "主動搭話：" + reasons.get(reason, reason or "這次已準備開口") + "\n"
        details = self.diagnostics[-1:] or [agent.get("error") or agent.get("last_error") or last.get("error") or voice.get("error")]
        if details and details[0]:
            text += "\n最近狀態：" + str(details[0])[-1500:]
        self.status_panel.configure(state="normal")
        self.status_panel.delete("1.0", "end")
        self.status_panel.insert("1.0", text)
        self.status_panel.configure(state="disabled")

    def _topmost_changed(self):
        self.root.attributes("-topmost", self.topmost_var.get())
        for window in (self.chat, self.settings, self.companion_settings, self.voice_library):
            if window and window.winfo_exists():
                window.attributes("-topmost", self.topmost_var.get())
        self._save()

    def _voice_changed(self):
        if not self.speak_var.get():
            self.stop()
        self._save()

    def prepare_voice(self):
        if self._prewarm_pending:
            return
        self._prewarm_pending = True
        self._prewarm_id = self.controller.prewarm()

    def _profile_changed(self):
        self.controller.set_profile(self.profile_var.get())
        if self._last_reply:
            # An explicit user choice changes the next retry. Otherwise the
            # failed reply keeps its original profile, including after restore.
            self._last_reply_profile = self.profile_var.get()
        self._refresh_status_panel()

    def _behavior_options_changed(self):
        self._presentation_interaction("behavior_settings")
        self.behavior.options(roaming=self.roaming_var.get(), mode=self.proactive_var.get(),
                              screen_enabled=self.screen_enabled_var.get(), screen_frequency=self.screen_frequency_var.get(),
                              screen_scope=self.screen_scope_var.get(), idle_actions=self.idle_actions_var.get(),
                              return_after_idle_seconds=self.return_idle_var.get())
        self._save()
        self._refresh_status_panel()

    def _skin_changed(self):
        self._presentation_interaction("skin")
        self._carry.reset()
        self._carry_sample = None
        x, y, _, _ = window_rect(self.native.hwnd)
        foot_x, foot_y = x + 12 + self.atlas.anchor[0], y + 10 + self.atlas.anchor[1]
        self.atlas = SpriteAtlas(self.root, self._skin_paths[self.skin_var.get()], self.size_var.get())
        self.width, self.height = self.atlas.width + 24, self.atlas.height + 20
        self.canvas.configure(width=self.width, height=self.height)
        self._animation = self._posture_animation("idle")
        self.canvas.itemconfigure(self.sprite_item, image=self.atlas.images[self.atlas.frame(self._animation, 0)])
        self._last_frame = None
        self._animation_start = time.monotonic()
        self.root.update_idletasks()
        self._move(round(foot_x - 12 - self.atlas.anchor[0]), round(foot_y - 10 - self.atlas.anchor[1]))
        self.behavior.reset_geometry(self.atlas)
        self.native.set_icon(self.atlas.write_icon(self.state_dir))
        self._build_motion_menu()
        self._save()

    def _size_changed(self):
        self._presentation_interaction("size")
        self._carry.reset()
        self._carry_sample = None
        x, y, _, bottom = window_rect(self.native.hwnd)
        old_anchor = self.atlas.anchor
        self.atlas.set_scale(self.size_var.get())
        self.width, self.height = self.atlas.width + 24, self.atlas.height + 20
        self.canvas.configure(width=self.width, height=self.height)
        self._last_frame = None
        self.root.update_idletasks()
        self._move(x + old_anchor[0] - self.atlas.anchor[0], y + old_anchor[1] - self.atlas.anchor[1])
        self.behavior.reset_geometry(self.atlas)
        self._save()

    def snapshot(self) -> dict:
        return {"schema": "desktop-pet-shell-v1", "pid": __import__("os").getpid(),
                "pet_rect": window_rect(self.native.hwnd), "frame_size": [self.atlas.width, self.atlas.height],
                "transparent_color": str(self.root.attributes("-transparentcolor")),
                "topmost": bool(self.root.attributes("-topmost")), "click_through": self.native.click_through,
                "tray_available": self.native.tray_available, "hotkey_available": self.native.hotkey_available,
                "chat_visible": bool(self.chat and self.chat.winfo_exists() and self.chat.state() != "withdrawn"),
                "turn_id": self.turn_id, "synthesis_pending": self._synthesis_pending,
                "agent_turn_id": self._reply.get("turn_id") if self._reply else None,
                "voice_retry_available": self._last_reply is not None,
                "skin": self.skin_var.get(), "sprite_manifest": str(self.atlas.path),
                "roaming_enabled": self.roaming_var.get(), "seated": self.seated_var.get(),
                "seat_button_label": self.seat_button_var.get(), "proactive_mode": self.proactive_var.get(),
                "screen_observation_enabled": self.screen_enabled_var.get(), "screen_scope": self.screen_scope_var.get(),
                "screen_frequency": self.screen_frequency_var.get(), "idle_actions_enabled": self.idle_actions_var.get(),
                "return_after_idle_seconds": self.return_idle_var.get(), "animation": self._animation,
                "monitor_layout": self.behavior.monitors,
                "motion_state": self._last_motion_state.value if self._last_motion_state else None,
                "proactive_operation": self._turn_is_proactive,
                "proactive_status": self.behavior.proactive.status(),
                "touch_operation": self._turn_is_touch,
                "touch_gesture_active": self._touch_gesture is not None,
                "carry_phase": self._carry_sample.phase if self._carry_sample else None,
                "carry_frame": self._carry_sample.frame if self._carry_sample else None,
                "carry_angle_degrees": round(self._carry_sample.angle_degrees, 2) if self._carry_sample else 0,
                "bubble_visible": bool(self.bubble and self.bubble.winfo_exists()),
                "playing": self.player.playing, "health": self.health, "diagnostics": self.diagnostics[-3:]}

    def capture(self, path: Path):
        self.root.update_idletasks()
        rects = [window_rect(self.native.hwnd)]
        for window in (self.chat, self.settings, self.bubble, self.voice_library):
            if window and window.winfo_exists() and window.state() != "withdrawn":
                rects.append(window_rect(hwnd_of(window)))
        left = min(rect[0] for rect in rects) - 16
        top = min(rect[1] for rect in rects) - 16
        right = max(rect[2] for rect in rects) + 16
        bottom = max(rect[3] for rect in rects) + 16
        # A temporary ordinary native window protects unrelated desktop content
        # in the evidence image. The pet is still a separate transparent window
        # composited by Windows, and the backdrop is removed immediately.
        backdrop = tk.Toplevel(self.root)
        backdrop.overrideredirect(True)
        backdrop.configure(bg="#26212D")
        backdrop.attributes("-toolwindow", True)
        backdrop.geometry(f"{right - left}x{bottom - top}+0+0")
        backdrop.update_idletasks()
        move_window(hwnd_of(backdrop), left, top, right - left, bottom - top, topmost=True)
        backdrop.lift()
        self.root.lift()
        for window in (self.chat, self.settings, self.bubble, self.voice_library):
            if window and window.winfo_exists() and window.state() != "withdrawn":
                window.lift()
        self.root.update()
        try:
            import ctypes
            ctypes.WinDLL("dwmapi").DwmFlush()
            capture_png(path, (left, top, right, bottom))
            snapshot = {**self.snapshot(), "capture_background": "temporary neutral native window, immediately removed"}
            path.with_suffix(".json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            backdrop.destroy()
        self._log("screenshot", path=str(path))

    def run_smoke(self):
        """Exercise actual native controls once, without invoking either model."""
        from types import SimpleNamespace
        self.open_chat()
        old = window_rect(self.native.hwnd)
        self._drag_start(SimpleNamespace(x_root=old[0] + 70, y_root=old[1] + 70, state=0x0001))
        self._drag_move(SimpleNamespace(x_root=old[0] + 38, y_root=old[1] + 50))
        self._drag_end(SimpleNamespace())
        assert window_rect(self.native.hwnd)[:2] != old[:2], "Drag did not move the native window"
        self.clickthrough_var.set(True)
        self.set_click_through()
        assert self.native.click_through, "Click-through was not available"
        self.recover()
        assert not self.native.click_through, "Recovery did not restore mouse input"
        self.stop()
        self._log("smoke_controls_passed", snapshot=self.snapshot())

    def close(self):
        if self.closing:
            return
        if self.config.get("final_screenshot"):
            try:
                self.capture(Path(self.config["final_screenshot"]))
            except Exception as exc:
                self._log("screenshot_error", error=str(exc))
        self.stop(silent=True)
        self._save()
        self.closing = True
        self._log("closing", snapshot=self.snapshot())
        self.native.close()
        for window in (self.chat, self.settings):
            if window and window.winfo_exists():
                window.withdraw()
        self.root.withdraw()
        self.controller.close()
        self._wait_for_close()

    def _wait_for_close(self):
        if self.controller.closed.is_set():
            self.instance.close()
            self.root.destroy()
        else:
            self.root.after(80, self._wait_for_close)
