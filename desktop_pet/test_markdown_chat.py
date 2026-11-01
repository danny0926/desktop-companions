"""Markdown/manual-image contracts with native Tk widgets and mocked CLI I/O.

No pet application, production state, model, browser or voice process is started.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass, is_dataclass
import hashlib
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import zipfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from desktop_pet.agent.actions import parse_user_action
from desktop_pet.agent.codex_subscription import CodexSubscriptionProvider
from desktop_pet.agent.performance import build_reply, ReplyFormatError, reply_schema
from desktop_pet.agent.providers import ProviderCancelled, ProviderImageUnsupported
from desktop_pet.agent.service import AgentService
from desktop_pet.app import PetApplication
from desktop_pet.attachments import AttachmentStore
from desktop_pet.contracts import AgentRequest, ManualImage
from desktop_pet.markdown_chat import image_references, MarkdownRenderer, visible_text
from desktop_pet.observation.screen import _png_from_bgra
from desktop_pet.shell.controller import ConversationController, Turn
from desktop_pet.shell.test_interaction import fake_app
from desktop_pet.shell.test_proactive_interruption import held_proactive


def _png(red=20):
    return _png_from_bgra(bytes((80, 120, red, 255)) * 40 * 24, 40, 24)


def _image(red=20):
    png = _png(red)
    return ManualImage(png_bytes=png, image_id=hashlib.sha256(png).hexdigest(), width=40, height=24)


def _response(**extra):
    return {"speech_text_ja": "一緒に見てみよう。", "subtitle_zh": "一起看看吧。",
            "attitude": "daily", "intensity": "light", "delivery": "natural", **extra}


class _Provider:
    mode, model, base_url = "codex_subscription", "gpt-6-luna", ""
    supports_manual_images = True

    def __init__(self):
        self.calls = []

    def complete_cancellable(self, messages, schema, **options):
        self.calls.append((messages, options))
        return json.dumps(_response(), ensure_ascii=False)


class AttachmentContracts(unittest.TestCase):
    def test_local_paths_order_durable_references_and_commonmark_boundaries(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first, second = root / "first picture (a).png", root / "second.png"
            first.write_bytes(_png(10))
            second.write_bytes(_png(240))
            store = AttachmentStore(root / "private" / "nino")
            # Identical text inside code is not an image. Angle-bracket paths
            # support Windows drives, spaces and parentheses without a browser.
            sample = f"![一](<{first.as_posix()}>)"
            source = f"`{sample}`\n\n# 看這裡\n{sample}，還有\n> ![二][picture]\n\n[picture]: <{second.as_posix()}>\n"
            normalized, images = store.prepare_markdown(source)
            self.assertEqual(len(images), 2)
            refs = image_references(normalized)
            self.assertEqual([ref.source for ref in refs], [image.reference for image in images])
            self.assertEqual([ref.alt for ref in refs], ["一", "二"])
            self.assertIn(f"`{sample}`", normalized)
            # Only the literal code example retains its path; real image
            # destinations/reference definitions have become durable IDs.
            self.assertEqual(normalized.count(first.as_posix()), 1)
            self.assertNotIn(second.as_posix(), normalized)
            self.assertEqual(store.load(images[0].reference).png_bytes, images[0].png_bytes)
            first.unlink()
            self.assertEqual(store.prepare_markdown(f"![]({images[0].reference})")[1][0].image_id, images[0].image_id)
            self.assertTrue(all(image.width <= 1600 and image.height <= 1600 for image in images))

    def test_invalid_missing_remote_excess_images_are_rejected_without_fetching(self):
        with tempfile.TemporaryDirectory() as temp:
            store = AttachmentStore(Path(temp) / "attachments")
            bad = Path(temp) / "broken.png"
            bad.write_bytes(b"not an image")
            for source in (f"![](<{bad.as_posix()}>)", "![](https://example.invalid/private.png)",
                           "![](missing-image.png)", "![](attachment:" + "0" * 64 + ")", "![](x.png)\n" * 5):
                with self.subTest(source=source), self.assertRaisesRegex(ValueError, "草稿已保留"):
                    store.prepare_markdown(source)
            self.assertEqual(store.prepare_markdown('`![](missing.png)`\n\n\\![x](missing.png)\n\n<img src="missing.png">')[1], ())

    def test_metadata_not_bytes_and_plain_speech_contract(self):
        image = _image()
        self.assertFalse(is_dataclass(image))
        with self.assertRaises(TypeError):
            asdict(image)
        @dataclass
        class Envelope:
            images: tuple
        serializable = json.dumps(asdict(Envelope((image,))))
        self.assertNotIn("PNG", serializable)
        self.assertNotIn("png_bytes", serializable)
        self.assertEqual(image.metadata()["reference"], image.reference)
        reply = build_reply(_response(display_text_ja="**一緒に**見てみよう。"), turn_id="r", provider_mode="codex_subscription")
        self.assertEqual(reply.speech_text_ja, "一緒に見てみよう。")
        self.assertEqual(reply.to_voice_request().text_ja, "一緒に見てみよう。")
        for display in ("別の話をしよう。", f"一緒に見てみよう。![]({image.reference})"):
            with self.assertRaises(ReplyFormatError):
                build_reply(_response(display_text_ja=display), turn_id="r", provider_mode="codex_subscription")
        for speech in ("# こんにちは。", "_こんにちは。_", "こんにちは。https://example.invalid/a.png", "（笑う）こんにちは。"):
            with self.assertRaises(ReplyFormatError):
                build_reply(_response(speech_text_ja=speech), turn_id="r", provider_mode="codex_subscription")
        self.assertEqual(parse_user_action("*摸頭*").kind, "headpat")
        self.assertIsNone(parse_user_action("**粗體**"))
        self.assertIsNone(parse_user_action("_文字_"))
        self.assertIsNone(parse_user_action("**你好**，*摸頭*"))

    def test_manual_images_current_request_only_and_memory_metadata(self):
        provider, images = _Provider(), (_image(30), _image(200))
        text = "第一張：![](" + images[0].reference + ")\n第二張：![](" + images[1].reference + ")"
        with tempfile.TemporaryDirectory() as temp, closing(AgentService(data_dir=temp, provider=provider)) as service:
            service.generate_reply(AgentRequest("with-images", text), images=images)
            self.assertEqual(provider.calls[0][1]["images"], images)
            service.generate_reply(AgentRequest("text-only", "你喜歡哪種顏色？"))
            self.assertNotIn("images", provider.calls[1][1])
            self.assertIn(images[0].reference, json.dumps(provider.calls[1][0]))
            memory = json.dumps(service.memory.recent_messages("default"), ensure_ascii=False)
            self.assertNotIn("png_bytes", memory)
            self.assertNotIn("base64", memory)
            with self.assertRaises(ProviderImageUnsupported):
                service.generate_reply(AgentRequest("missing-payload", text))
        provider.supports_manual_images = False
        with tempfile.TemporaryDirectory() as temp, closing(AgentService(data_dir=temp, provider=provider)) as service:
            with self.assertRaises(ProviderImageUnsupported):
                service.generate_reply(AgentRequest("unsupported", text), images=images)

    def test_cli_attaches_in_order_and_cleans_success_cancel_and_unsupported(self):
        images, observed = (_image(10), _image(240)), []
        for outcome in ("success", "cancel", "unsupported"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as temp:
                cancel = threading.Event()

                def spawn(arguments, **options):
                    paths = [Path(arguments[index + 1]) for index, value in enumerate(arguments) if value == "--image"]
                    observed.append(paths)
                    self.assertEqual([path.read_bytes() for path in paths], [image.png_bytes for image in images])
                    self.assertTrue(all(path.parent == Path(options["cwd"]) for path in paths))
                    self.assertIn("--ephemeral", arguments)
                    self.assertNotIn("OPENAI_API_KEY", options["env"])
                    class Process:
                        returncode = 1 if outcome == "unsupported" else 0
                        def poll(self):
                            return self.returncode
                        def wait(self, **kwargs):
                            return self.returncode
                        def communicate(self, prompt=None, **kwargs):
                            if outcome == "cancel":
                                cancel.set()
                            payload = json.loads(prompt)
                            metadata = payload["attached_manual_images"]
                            self_test.assertEqual([item["reference"] for item in metadata], [image.reference for image in images])
                            self_test.assertEqual([item["attachment_number"] for item in metadata], [1, 2])
                            self_test.assertNotIn("attached_screen_observation", payload)
                            self_test.assertNotIn(str(paths[0]), prompt)
                            self_test.assertNotIn("png_bytes", prompt)
                            if outcome == "unsupported":
                                return "", "images not supported by selected model"
                            return '\n'.join((json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "{}"}}),
                                              json.dumps({"type": "turn.completed"}))), ""
                    self_test = self
                    return Process()

                with patch("desktop_pet.agent.codex_subscription._command", return_value=["codex"]), \
                        patch("desktop_pet.agent.codex_subscription.subprocess.Popen", side_effect=spawn):
                    provider = CodexSubscriptionProvider({"model": "gpt-6-luna", "codex": {"temporary_root": temp}})
                    kwargs = dict(timeout_seconds=10, turn_id="manual", cancel_event=cancel, images=images)
                    if outcome == "success":
                        self.assertEqual(provider.complete_cancellable([], reply_schema(), **kwargs), "{}")
                        self.assertEqual(provider.last_run["manual_image_count"], 2)
                        self.assertNotIn("png_bytes", repr(provider.last_run))
                    else:
                        error = ProviderCancelled if outcome == "cancel" else ProviderImageUnsupported
                        with self.assertRaises(error):
                            provider.complete_cancellable([], reply_schema(), **kwargs)
                    provider.close()
                self.assertTrue(all(not path.exists() for path in observed[-1]))
                self.assertEqual(list(Path(temp).iterdir()), [])

    def test_controller_delivers_typed_images_but_no_text_only_keyword(self):
        calls, done = [], threading.Event()
        class Agent:
            def generate_reply(self, request, **options):
                calls.append((request, options))
                return build_reply(_response(), turn_id=request.turn_id, provider_mode="codex_subscription")
            def record_delivery(self, *args, **kwargs):
                pass
        def emit(event):
            if event["type"] == "finished":
                done.set()
        controller = ConversationController({"conversation_id": "test"}, emit, agent=Agent())
        try:
            image = _image()
            controller.start(f"![]({image.reference})", speak=False, images=(image,))
            self.assertTrue(done.wait(3))
            self.assertEqual(calls[0][1]["images"], (image,))
            done.clear()
            controller.start("こんにちは。", speak=False)
            self.assertTrue(done.wait(3))
            self.assertEqual(calls[1][1], {})
        finally:
            controller.close()

    def test_app_import_does_not_initialize_heavy_optional_libraries(self):
        result = subprocess.run([sys.executable, "-c", "import sys; import desktop_pet.app; "
                                 "assert not any(name in sys.modules for name in ('PIL', 'torch', 'transformers'))"],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


class NativeTkMarkdownContracts(unittest.TestCase):
    def setUp(self):
        self._paste_apps = []
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.directory = Path(self.temp.name)

    def _paste_app(self, draft=""):
        app = fake_app()
        app.root = self.root
        app.events = queue.Queue()
        app.status = tk.StringVar(self.root, "")
        app.state_dir, app.state_path = self.directory, self.directory / "window.json"
        app.native = SimpleNamespace(hwnd=self.root.winfo_id())
        config = self.directory / "character.json"
        config.write_text('{"character_id":"nino-paste-fixture"}', encoding="utf-8")
        app.config["agent_config"] = str(config)
        app._save = PetApplication._save.__get__(app)
        app.input_box = tk.Text(self.root, undo=True)
        app.input_box.insert("1.0", draft)
        self._paste_apps.append(app)
        return app

    def tearDown(self):
        for app in self._paste_apps:
            for worker in getattr(app, "_clipboard_paste_threads", ()):
                if worker.is_alive():
                    worker.join(3)
            widget = getattr(app, "input_box", None)
            if widget is not None and widget.winfo_exists():
                for sequence in ("<<Paste>>", "<Control-Key-v>", "<Control-Key-V>",
                                 "<Shift-Key-Insert>", "<Button-3>", "<KeyPress>"):
                    widget.unbind(sequence)
            app.input_box = None
            app.status = None
            app._save = None
            app.root = None
        if hasattr(self, "root"):
            self.root.destroy()
        if hasattr(self, "temp"):
            self.temp.cleanup()

    def test_real_text_tags_images_and_no_model_or_html_image_loading(self):
        path = self.directory / "synthetic.png"
        path.write_bytes(_png())
        store = AttachmentStore(self.directory / "nino")
        image = store.import_file(path)
        text = tk.Text(self.root)
        renderer = MarkdownRenderer(text, store.thumbnail)
        source = f'# 標題\n**粗體**、_斜體_，[連結](https://example.invalid)。\n\n- 一\n- 二\n\n> 引用\n\n```txt\n程式碼\n```\n\n![合成圖片]({image.reference})\n<img src="https://example.invalid/no.png">'
        renderer.append(source, allow_images=True)
        self.root.update_idletasks()
        visible = text.get("1.0", "end-1c")
        self.assertIn("粗體、斜體，連結。", visible)
        self.assertIn("• 一", visible)
        self.assertIn('<img src="https://example.invalid/no.png">', visible)
        for tag in ("md_user_h1", "md_user_bold", "md_user_italic", "md_quote", "md_code", "md_link"):
            self.assertTrue(text.tag_ranges(tag), tag)
        self.assertEqual(len(text.image_names()), 1)
        text.delete("1.0", "end")
        renderer.clear()
        renderer.append(f"![never-autoload]({image.reference})", "subtitle")
        self.assertEqual(len(text.image_names()), 0)
        self.assertIn("未載入", text.get("1.0", "end"))

    def test_native_composer_cursor_draft_image_only_send_and_error_retention(self):
        app = fake_app()
        app.root = self.root
        app.events = queue.Queue()
        app.state_dir, app.state_path = self.directory, self.directory / "window.json"
        app.native = SimpleNamespace(hwnd=self.root.winfo_id())
        config = self.directory / "character.json"
        config.write_text('{"character_id":"nino-fixture"}', encoding="utf-8")
        app.config["agent_config"] = str(config)
        app._save = PetApplication._save.__get__(app)
        app.input_box = tk.Text(self.root, undo=True)
        app.input_box.insert("1.0", "前，後。")
        app.input_box.mark_set("insert", "1.2")
        image = self.directory / "chosen picture.png"
        image.write_bytes(_png())
        app._insert_chat_images((str(image),))
        app._handle_event(app.events.get(timeout=10))
        draft = app._chat_draft()
        self.assertTrue(draft.startswith("前，![圖片 1](attachment:"))
        self.assertTrue(draft.endswith("後。"))
        self.assertEqual(app.controller.stops, 0)
        saved = json.loads(app.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["chat_draft"], draft)
        self.assertEqual(len(saved["chat_draft_image_ids"]), 1)
        self.assertTrue((self.directory / "attachments" / "nino-fixture").is_dir())
        reloaded_store = AttachmentStore(self.directory / "attachments" / "nino-fixture")
        self.assertEqual(len(reloaded_store.prepare_markdown(saved["chat_draft"])[1]), 1)
        app.input_box.delete("1.0", "end")
        app.input_box.insert("1.0", "![](https://example.invalid/missing.png)")
        bad = app._chat_draft()
        app.send_input()
        app._handle_event(app.events.get(timeout=10))
        self.assertEqual(app._chat_draft(), bad)
        self.assertIn("草稿已保留", app.status.get())
        self.assertEqual(app.controller.starts, [])
        app.input_box.delete("1.0", "end")
        ref = image_references(draft)[0].source
        app.input_box.insert("1.0", f"![]({ref})")
        app.send_input()
        app._handle_event(app.events.get(timeout=10))
        self.assertEqual(app._chat_draft(), "")
        self.assertEqual(len(app.controller.starts), 1)
        self.assertEqual(app.controller.starts[0][1]["images"][0].reference, ref)
        self.assertEqual(app._transcript_parts[-1][0], "user")

    def test_clipboard_text_replaces_selection_once_and_undo_restores_draft(self):
        app = self._paste_app("\u524d\u820a\u5f8c")
        app.input_box.tag_add("sel", "1.1", "1.2")
        app.input_box.mark_set("insert", "1.2")
        with patch("desktop_pet.app.read_clipboard", return_value=SimpleNamespace(
                file_paths=(), image_bytes=None, image_format=None, text="\u65b0\n\u884c")):
            PetApplication._paste_clipboard(app)
        self.assertEqual(app.input_box.get("1.0", "end-1c"), "\u524d\u65b0\n\u884c\u5f8c")
        self.assertEqual(app.controller.stops, 0)
        app.input_box.edit_undo()
        self.assertEqual(app.input_box.get("1.0", "end-1c"), "\u524d\u820a\u5f8c")

        app.input_box.mark_set("insert", "1.1")
        app.input_box.bind("<<Paste>>", app._paste_clipboard)
        with patch("desktop_pet.app.read_clipboard", return_value=SimpleNamespace(
                file_paths=(), image_bytes=None, image_format=None, text="X")):
            PetApplication._paste_clipboard(app)
        self.assertEqual(app.input_box.get("1.0", "end-1c"), "\u524dX\u820a\u5f8c")
        self.assertEqual(app.input_box.index("insert"), "1.2")

        with patch("desktop_pet.app.read_clipboard", return_value=SimpleNamespace(
                file_paths=(), image_bytes=None, image_format=None, text="Q")):
            app.input_box.event_generate("<<Paste>>", when="now")
        self.assertEqual(app.input_box.get("1.0", "end-1c"), "\u524dXQ\u820a\u5f8c")

    def test_physical_paste_keys_use_one_mocked_snapshot_pipeline(self):
        app = self._paste_app("")
        bindings = ("<Control-Key-v>", "<Control-Key-V>", "<Shift-Key-Insert>")
        app._bind_paste_shortcuts(app.input_box)
        clips = [SimpleNamespace(file_paths=(), image_bytes=None, image_format=None, text=value)
                 for value in ("v", "V", "I")]
        with patch("desktop_pet.app.read_clipboard", side_effect=clips) as reader:
            for sequence in bindings:
                script = app.input_box.bind(sequence)
                self.assertTrue(script)
            app.input_box.bind("<<Paste>>", app._paste_clipboard)
            self.assertTrue(app.input_box.bind("<<Paste>>"))
            PetApplication._paste_clipboard(app)
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(app._chat_draft(), "v")

    def test_agent_request_accepts_the_composer_32k_limit(self):
        self.assertEqual(len(AgentRequest("long", "x" * 32000).input_text), 32000)
        with self.assertRaises(ValueError):
            AgentRequest("too-long", "x" * 32001)

    def test_composer_send_accepts_more_than_legacy_4000_character_limit(self):
        app = self._paste_app()
        sent = []
        app._start_turn = lambda text, **kwargs: sent.append(text)
        self.assertTrue(app.send_text("x" * 5000))
        self.assertEqual(len(sent[0]), 5000)
        self.assertFalse(app.send_text("x" * 32001))
        self.assertEqual(len(sent), 1)

    def test_queued_image_pastes_keep_order_track_typing_and_block_send(self):
        app = self._paste_app("ab")
        app.input_box.mark_set("insert", "1.1")
        entered = threading.Event()
        release = threading.Event()
        calls = []
        def import_bytes(payload, *, image_format=None):
            calls.append(payload)
            if len(calls) == 1:
                entered.set()
                self.assertTrue(release.wait(3))
            return SimpleNamespace(image_id=("a" if payload == b"one" else "b") * 64)
        app._attachments = SimpleNamespace(import_bytes=import_bytes)
        clips = [SimpleNamespace(file_paths=(), image_bytes=b"one", image_format="png", text=None),
                 SimpleNamespace(file_paths=(), image_bytes=b"two", image_format="png", text=None)]
        with patch("desktop_pet.app.read_clipboard", side_effect=clips):
            PetApplication._paste_clipboard(app)
            self.assertTrue(entered.wait(2))
            PetApplication._paste_clipboard(app)
        self.assertEqual(len(app._clipboard_paste_queue), 1)
        app.input_box.insert("insert", "X")
        app.send_input()
        self.assertEqual(app.controller.starts, [])
        release.set()
        app._handle_event(app.events.get(timeout=3))
        app._handle_event(app.events.get(timeout=3))
        self.assertEqual(len(calls), 2)
        draft = app._chat_draft()
        self.assertIn("a" * 64, draft)
        self.assertIn("b" * 64, draft)
        self.assertLess(draft.index("a" * 64), draft.index("b" * 64))
        self.assertTrue(draft.startswith("aX![圖片](attachment:"))
        self.assertTrue(draft.endswith("b"))

    def test_clipboard_paste_queue_rejects_ninth_snapshot_before_reading(self):
        app = self._paste_app("keep draft")
        entered, release = threading.Event(), threading.Event()
        def import_bytes(payload, *, image_format=None):
            entered.set()
            self.assertTrue(release.wait(3))
            return SimpleNamespace(image_id="d" * 64)
        app._attachments = SimpleNamespace(import_bytes=import_bytes)
        clip = SimpleNamespace(file_paths=(), image_bytes=b"dib-payload", image_format="dibv5", text=None)
        with patch("desktop_pet.app.read_clipboard", return_value=clip) as reader:
            for _ in range(8):
                PetApplication._paste_clipboard(app)
            self.assertTrue(entered.wait(2))
            self.assertEqual(reader.call_count, 8)
            self.assertEqual(len(app._clipboard_paste_queue), 7)
            serial = app._clipboard_paste_serial
            PetApplication._paste_clipboard(app)
            self.assertEqual(reader.call_count, 8)
        self.assertEqual(app._clipboard_paste_serial, serial)
        self.assertEqual(app._chat_draft(), "keep draft")
        self.assertIn("最多 8 項", app.status.get())
        release.set()

    def test_async_image_does_not_replace_changed_selection_and_rechecks_limit(self):
        app = self._paste_app("abc")
        app.input_box.tag_add("sel", "1.1", "1.2")
        app.input_box.mark_set("insert", "1.2")
        app._attachments = SimpleNamespace(import_bytes=lambda *args, **kwargs: SimpleNamespace(image_id="a" * 64))
        clip = SimpleNamespace(file_paths=(), image_bytes=b"image", image_format="png", text=None)
        with patch("desktop_pet.app.read_clipboard", return_value=clip):
            PetApplication._paste_clipboard(app)
        event = app.events.get(timeout=3)
        app.input_box.tag_remove("sel", "1.0", "end")
        app.input_box.tag_add("sel", "1.2", "1.3")
        app._handle_event(event)
        self.assertEqual(app._chat_draft(), "abc")
        self.assertIn("選取的文字已變更", app.status.get())

        app.input_box.tag_remove("sel", "1.0", "end")
        app.input_box.delete("1.0", "end")
        refs = "\n".join(f"![](attachment:{i:064x})" for i in range(4))
        app.input_box.insert("1.0", refs)
        app.input_box.mark_set("insert", "end-1c")
        with patch("desktop_pet.app.read_clipboard", return_value=clip):
            PetApplication._paste_clipboard(app)
        app._handle_event(app.events.get(timeout=3))
        self.assertEqual(len(image_references(app._chat_draft())), 4)
        self.assertIn("最多附加 4 張圖片", app.status.get())

    def test_image_paste_keeps_meaningful_rich_text_and_filters_file_metadata(self):
        app = self._paste_app("")
        app._attachments = SimpleNamespace(import_bytes=lambda *args, **kwargs: SimpleNamespace(image_id="c" * 64))
        clip = SimpleNamespace(file_paths=(), image_bytes=b"bitmap", image_format="dibv5",
                               text="A1\tB1\nA2\tB2\nhttps://example.com\t120\nhttps://example.invalid/metadata")
        app.input_box.mark_set("insert", "1.0")
        with patch("desktop_pet.app.read_clipboard", return_value=clip):
            PetApplication._paste_clipboard(app)
        app._handle_event(app.events.get(timeout=3))
        draft = app._chat_draft()
        self.assertIn("attachment:" + "c" * 64, draft)
        self.assertIn("A1\tB1\nA2\tB2", draft)
        self.assertIn("https://example.com\t120", draft)
        self.assertNotIn("https://example.invalid/metadata", draft)

    def test_attachment_image_cache_deduplicates_repeated_clipboard_bytes(self):
        store = AttachmentStore(self.directory / "attachments" / "nino-paste-fixture")
        png = _png()
        first = store.import_bytes(png, image_format="png")
        second = store.import_bytes(png, image_format="png")
        self.assertEqual(first.reference, second.reference)
        self.assertEqual(len(list(store.root.glob("*.thumb.png"))), 1)

    def test_docx_and_pdf_clipboard_files_insert_extracted_text(self):
        app = self._paste_app("")
        docx = self.directory / "selection.docx"
        xml = ('<?xml version="1.0" encoding="UTF-8"?>'
               '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
               '<w:body><w:p><w:r><w:t>DOCX text</w:t></w:r></w:p></w:body></w:document>')
        with zipfile.ZipFile(docx, "w") as archive:
            archive.writestr("word/document.xml", xml)
        app.input_box.mark_set("insert", "1.0")
        with patch("desktop_pet.app.read_clipboard", return_value=SimpleNamespace(
                file_paths=(str(docx),), image_bytes=None, image_format=None, text=None)):
            PetApplication._paste_clipboard(app)
        app._handle_event(app.events.get(timeout=3))
        self.assertIn("DOCX text", app._chat_draft())

        pdf = self.directory / "selection.pdf"
        pdf.write_bytes(b"%PDF fixture")
        fake_reader = SimpleNamespace(is_encrypted=False,
                                      pages=[SimpleNamespace(extract_text=lambda: "PDF text")])
        with patch("desktop_pet.app.read_clipboard", return_value=SimpleNamespace(
                file_paths=(str(pdf),), image_bytes=None, image_format=None, text=None)), \
             patch("pypdf.PdfReader", return_value=fake_reader):
            PetApplication._paste_clipboard(app)
            app._handle_event(app.events.get(timeout=3))
        self.assertIn("PDF text", app._chat_draft())

    def test_readonly_paste_draft_does_not_invalidate_held_nonvisual_proactive_turn(self):
        with held_proactive() as (app, voice, events, audio_ready, operation):
            app.input_box = tk.Text(self.root, undo=True)
            app.events = queue.Queue()
            app._draft_text = ""
            app._save = lambda: None
            app.input_box.mark_set("insert", "1.0")
            clipboard = SimpleNamespace(file_paths=(), image_bytes=None,
                                        image_format=None, text="pasted draft")
            with patch("desktop_pet.app.read_clipboard", return_value=clipboard), \
                 patch("desktop_pet.app.hwnd_of", return_value=1):
                PetApplication._paste_clipboard(app)
            self.assertEqual(app._chat_draft(), "pasted draft")
            self.assertEqual(app._last_manual_at, float("-inf"))
            app.behavior.delivery_busy = app._busy_state(time.monotonic(), delivery=True)
            self.assertFalse(app.behavior.delivery_busy.input_active)
            self.assertTrue(app.controller.allow_delivery(operation))

            app._turn_complete = True
            app._synthesis_pending = False
            self.assertTrue(app._busy_state(time.monotonic(), delivery=True).input_active)
            app._turn_complete = False
            app._synthesis_pending = True
            voice.resume.set()
            self.assertTrue(audio_ready.wait(2), events)
            self.assertFalse(any(event["type"] == "proactive_discarded" for event in events))
            app.input_box.destroy()


if __name__ == "__main__":
    unittest.main()
