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
import tkinter as tk
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
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)

    def tearDown(self):
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


if __name__ == "__main__":
    unittest.main()
