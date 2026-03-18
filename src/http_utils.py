"""共通HTTPユーティリティ"""
import json
import re
import urllib.request
from typing import Any, Dict


def _http_get_json(url: str, timeout: int = 10) -> Dict[str, Any]:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def _http_post_json(url: str, payload: Dict[str, Any], timeout: int = 20) -> Dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def _norm_query(q: str) -> str:
    return re.sub(r"\s+", " ", (q or "").strip())


def _yield_text_event(text: str):
    """Lambda側が採用する Bedrock Converse Stream形式に寄せて出す。"""
    yield {"event": {"contentBlockDelta": {"delta": {"text": text}}}}
    yield {"event": {"contentBlockStop": {}}}
    yield {"event": {"messageStop": {}}}
    yield "[DONE]"
