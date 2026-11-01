"""Operate the resident service, inspect admission, or synthesize short sentences."""
from __future__ import annotations

import argparse
import json

from .service import VoiceService, load_config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--health", action="store_true")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--text")
    parser.add_argument("--warm-text", help="A second short sentence in the same resident session.")
    parser.add_argument("--prewarm", action="store_true", help="Load resident assets without generating speech.")
    parser.add_argument("--profile", choices=("quality_40", "interactive_6_sway"))
    parser.add_argument("--caption", default="参照音声と同じ声。近い距離で自然に、やさしく話す。")
    args = parser.parse_args()
    with VoiceService(args.config, profile=args.profile) as service:
        if args.serve:
            from .http import serve
            serve(service, port=args.port)
            return 0
        if args.prewarm:
            result = service.prewarm(timeout=float(service.config["request_timeout_seconds"]) + 5)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if not args.text:
                return 0 if result["status"] == "ready" else 2
        if args.health or not args.text:
            from .resources import staged_cold_requirement
            print(json.dumps({**service.health(), "cold_admission": staged_cold_requirement(service.config)}, ensure_ascii=False), flush=True)
            return 0
        service.begin_turn("cli")
        successful = True
        for segment, text in enumerate(filter(None, (args.text, args.warm_text))):
            result = service.synthesize({"text_ja": text, "caption_ja": args.caption,
                                         "turn_id": "cli", "segment_id": str(segment)},
                                        timeout=float(service.config["request_timeout_seconds"]) + 5)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            successful = successful and result["status"] == "generated"
            if result["status"] != "generated":
                break
        return 0 if successful else 2


if __name__ == "__main__":
    raise SystemExit(main())
