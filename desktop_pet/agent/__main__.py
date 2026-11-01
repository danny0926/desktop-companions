"""Small local command line entry point for the shared AgentService."""

from __future__ import annotations

import argparse
import json
import sys
import uuid

from desktop_pet.contracts import AgentRequest
from .providers import ProviderError
from .service import AgentService


def main() -> int:
    parser = argparse.ArgumentParser(description="Nino text agent: Codex subscription, local memory and Japanese acting metadata")
    parser.add_argument("--config", help="Character/provider JSON path")
    parser.add_argument("--data-dir", help="SQLite state directory")
    parser.add_argument("--text", help="Generate one reply; omitted to use JSONL stdin")
    parser.add_argument("--conversation", default="default")
    parser.add_argument("--turn-id")
    parser.add_argument("--health", action="store_true")
    parser.add_argument("--memories", action="store_true")
    parser.add_argument("--sources", action="store_true")
    args = parser.parse_args()
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        service = AgentService(args.config, args.data_dir)
        if args.health:
            print(json.dumps(service.health(), ensure_ascii=False, indent=2))
        elif args.memories:
            print(json.dumps(service.list_memories(), ensure_ascii=False, indent=2))
        elif args.sources:
            print(json.dumps(service.list_sources(), ensure_ascii=False, indent=2))
        elif args.text is not None:
            request = AgentRequest(args.turn_id or uuid.uuid4().hex, args.text, args.conversation)
            print(json.dumps(service.generate_reply(request).to_dict(), ensure_ascii=False, indent=2))
        else:
            for line in sys.stdin:
                if not line.strip():
                    continue
                request = AgentRequest.from_dict(json.loads(line))
                print(json.dumps(service.generate_reply(request).to_dict(), ensure_ascii=False), flush=True)
        return 0
    except (ProviderError, ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
