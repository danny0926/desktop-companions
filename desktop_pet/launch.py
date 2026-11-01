"""CLI used by python -m desktop_pet and the hidden-console PowerShell launcher."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import traceback


ROOT = Path(__file__).resolve().parents[1]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="二乃像素桌寵（Windows／Tk 8.6）")
    parser.add_argument("--config", default=str(ROOT / "config/desktop_pet_app.json"))
    parser.add_argument("--chat", action="store_true", help="同時開啟聊天視窗")
    parser.add_argument("--no-voice", action="store_true", help="只顯示回覆，不啟動本機 TTS")
    parser.add_argument("--message", help="以正常對話流程傳送一則文字")
    parser.add_argument("--exit-when-done", action="store_true", help="回覆與播放完成後退出")
    parser.add_argument("--exit-after", type=float, default=0, help="指定秒數後退出")
    parser.add_argument("--screenshot", help="退出前擷取實際桌寵與聊天視窗 PNG")
    parser.add_argument("--capture-after", type=float, help="另外在指定秒數擷取相同 PNG")
    parser.add_argument("--smoke", action="store_true", help="只驗證真視窗控制，不呼叫對話／語音模型")
    args = parser.parse_args(argv)
    if os.name != "nt":
        parser.error("The desktop shell requires Windows. Agent/voice contracts can be used separately.")
    from .shell.win32 import InstanceGuard, enable_dpi_awareness
    enable_dpi_awareness()
    instance = InstanceGuard(ROOT)
    if instance.already_running:
        instance.close()
        return 0
    try:
        path = Path(args.config).resolve()
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        for key in ("sprite_manifest", "agent_config", "agent_data_dir", "voice_config", "state_dir", "event_log"):
            value = Path(config[key])
            config[key] = str((value if value.is_absolute() else ROOT / value).resolve())
        config["no_voice"] = args.no_voice or args.smoke
        config["exit_when_done"] = args.exit_when_done
        if args.screenshot:
            config["final_screenshot"] = str(Path(args.screenshot).resolve())
        import tkinter as tk
        from .app import PetApplication
        root = tk.Tk()
        app = PetApplication(root, config, instance, smoke=args.smoke)
        if args.chat or args.message or config.get("open_chat_on_start"):
            root.after(120, app.open_chat)
        if args.smoke:
            root.after(350, app.run_smoke)
        if args.message and not args.smoke:
            root.after(250, lambda: app.send_text(args.message))
        if args.screenshot and args.capture_after is not None:
            root.after(max(100, round(args.capture_after * 1000)), lambda: app.capture(Path(config["final_screenshot"])))
        exit_after = args.exit_after or (4.0 if args.smoke else 0)
        if exit_after:
            root.after(max(500, round(exit_after * 1000)), app.close)
        root.mainloop()
        return app.exit_code
    except Exception:
        instance.close()
        log = ROOT / "outputs/pet-shell-v1/startup-error.txt"
        log.parent.mkdir(parents=True, exist_ok=True)
        error = traceback.format_exc()
        log.write_text(error, encoding="utf-8")
        if sys.stderr:
            print(error, file=sys.stderr)
        if not args.smoke:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, f"桌寵無法啟動。詳細資訊：\n{log}", "二乃", 0x10)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
