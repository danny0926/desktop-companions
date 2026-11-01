"""Translate voice resource evidence into concise, truthful UI copy."""
from __future__ import annotations

RESOURCE_LABELS = {
    "physical_available_bytes": "可用記憶體",
    "commit_available_bytes": "系統提交餘量",
    "gpu_free_bytes": "顯示記憶體",
}
CATEGORY_KEYS = {"host_ram": "physical_available_bytes", "host_commit": "commit_available_bytes",
                 "gpu_memory": "gpu_free_bytes"}


def resource_issue(value: dict) -> dict | None:
    waiting = value.get("backend_stage") == "waiting_resources" or value.get("status") == "waiting_resources"
    detail = value.get("resource_wait") if waiting else None
    if not detail:
        detail = value.get("resource_block")
    if not detail and value.get("voice_state", value.get("backend_stage")) == "unloaded_for_resources":
        detail = {"phase": "warm_synthesis", "stage": "idle_resource_cleanup", **value.get("resource_unload", {})}
    if not detail:
        last = value.get("last_result", {})
        if last.get("status") == "blocked_memory":
            detail = last.get("resource_block")
    if not isinstance(detail, dict):
        return None
    observed, required = detail.get("observed", {}), detail.get("required", {})
    failed = [key for key, amount in required.items() if isinstance(amount, (int, float)) and
              (not isinstance(observed.get(key), (int, float)) or observed[key] < amount)]
    category = detail.get("category")
    if not category:
        category = next((name for name, key in CATEGORY_KEYS.items() if key in failed), "probe")
    phase = detail.get("phase")
    stage = str(detail.get("stage", ""))
    if not phase:
        phase = "warm_synthesis" if stage in {"warm_synthesis", "decode", "cpu_watermark", "watchdog"} else "cold_load"
    when = "運行中" if phase == "warm_synthesis" else "載入前" if stage in {"process_start", "cold_load", "cold_admission", ""} else "載入中"
    subject = RESOURCE_LABELS.get(CATEGORY_KEYS.get(category), "資源檢查")
    if category == "probe":
        headline = f"{when}確認語音所需資源" if waiting else "暫時無法確認語音所需資源"
    else:
        headline = f"{when}等待{subject}" if waiting else f"{when}{subject}暫時不足"
    return {"waiting": waiting, "phase": phase, "category": category, "headline": headline,
            "observed": observed, "required": required, "detail": detail}


def resource_status(value: dict) -> str | None:
    issue = resource_issue(value)
    if issue is None:
        return None
    return issue["headline"] + ("，請稍候…" if issue["waiting"] else "。可按「重試語音」。")


def resource_details(value: dict) -> str:
    issue = resource_issue(value)
    if issue is None:
        return ""
    lines = [issue["headline"]]
    for key, label in RESOURCE_LABELS.items():
        available, required = issue["observed"].get(key), issue["required"].get(key)
        if required is None:
            continue
        available_text = f"{available / 1024 ** 2:.1f} MiB" if isinstance(available, (int, float)) else "未知"
        shortfall = max(0.0, required - available) if isinstance(available, (int, float)) else None
        missing = f"；尚差 {shortfall / 1024 ** 2:.1f} MiB" if shortfall else ""
        lines.append(f"{label}：目前 {available_text}／需要 {required / 1024 ** 2:.1f} MiB{missing}")
    detail = issue["detail"]
    elapsed = detail.get("elapsed_seconds", detail.get("waited_seconds"))
    limit = detail.get("timeout_seconds")
    if isinstance(elapsed, (int, float)):
        lines.append(f"已等待 {elapsed:.1f} 秒" + (f"／最多 {limit:g} 秒" if isinstance(limit, (int, float)) else ""))
    return "\n".join(lines)
