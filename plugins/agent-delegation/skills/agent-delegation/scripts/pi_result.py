"""Reconcile pi-acp's end_turn with the exact native Pi branch and mission.

pi-acp 0.0.33 can report end_turn after a provider error. Never infer Pi
success from that transport status or from human-readable error strings.
"""

import json
from pathlib import Path


def reconcile_pi_result(parsed: dict, request: dict, session_map: str | None = None) -> None:
    if parsed.get("stop_reason") != "end_turn":
        return
    parsed["transport_stop_reason"] = "end_turn"
    parsed["stop_reason"] = "unknown"
    parsed["pi_native_result"] = {"verified": False}
    try:
        map_path = Path(session_map) if session_map else Path.home() / ".pi/pi-acp/session-map.json"
        mapping = json.loads(map_path.read_text(encoding="utf-8"))
        record = mapping["sessions"][parsed["acp_session_id"]]
        if Path(record["cwd"]).resolve() != Path(request["cwd"]).resolve():
            return
        path = Path(record["sessionFile"])
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        entries = {row["id"]: row for row in rows if "id" in row and row.get("type") != "session"}
        current = next((row for row in reversed(rows) if row.get("id") in entries), None)
        visited = set()
        assistant = None
        while current and current["id"] not in visited:
            visited.add(current["id"])
            message = current.get("message", {})
            if message.get("role") == "assistant" and assistant is None:
                assistant = message
            if message.get("role") == "user":
                content = message.get("content", [])
                text = content if isinstance(content, str) else "".join(
                    part.get("text", "") for part in content if part.get("type") == "text")
                if request["delegation_id"] in text and assistant is not None:
                    stop = assistant.get("stopReason")
                    parsed["stop_reason"] = {"stop": "end_turn", "error": "error",
                                             "aborted": "cancelled", "length": "max_tokens"}.get(stop, "unknown")
                    parsed["pi_native_result"] = {"verified": stop in ("stop", "error", "aborted", "length"),
                                                  "stop_reason": stop, "session_file": str(path),
                                                  "mission_entry_id": current["id"]}
                    return
                return
            current = entries.get(current.get("parentId"))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        # Unknown native outcome is incomplete, never a transport-derived success.
        return
