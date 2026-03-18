"""セッション単位のインメモリ状態管理"""
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from http_utils import _norm_query

_LOCATION_CMD_PREFIX = "__set_location__"

# PoC: Dynamoなしで「最後の位置情報」をセッション単位に保持
_SESSION_LOCATION: Dict[str, Dict[str, Any]] = {}
# 直近の店舗候補（1件のみ保持）
_SESSION_RECOMMENDATIONS: Dict[str, List[Dict[str, Any]]] = {}
# AgentCore Memory: 確定した好み履歴（ジャンル・店名）をインメモリ蓄積
_SESSION_PREFERENCES: Dict[str, List[str]] = {}
# 確認待ち店舗: _set_recommendations 時に設定 → invoke_agent がマーカー送出後にクリア
_SESSION_SHOP_READY: Dict[str, Optional[Dict[str, Any]]] = {}


# ========== Location ==========

def _get_location(session_id: str) -> Optional[Dict[str, Any]]:
    return _SESSION_LOCATION.get(session_id)


def _set_location(session_id: str, lat: float, lng: float, title: str = "", address: str = "") -> None:
    _SESSION_LOCATION[session_id] = {
        "lat": float(lat),
        "lng": float(lng),
        "title": _norm_query(title),
        "address": _norm_query(address),
    }


# ========== Recommendations ==========

def _set_recommendations(session_id: str, shops: List[Dict[str, Any]], limit: int = 1) -> None:
    _SESSION_RECOMMENDATIONS[session_id] = list(shops[: max(1, int(limit))])
    # 推薦が設定されたら確認待ちとしても保存（LLMを経由させないため）
    if shops:
        _SESSION_SHOP_READY[session_id] = shops[0]
        print(f"[DEBUG _set_recommendations] session_id={session_id} shop={shops[0].get('name')}", flush=True)


def _clear_recommendations(session_id: str) -> None:
    _SESSION_RECOMMENDATIONS.pop(session_id, None)
    _SESSION_SHOP_READY.pop(session_id, None)


def _pop_shop_ready(session_id: str) -> Optional[Dict[str, Any]]:
    """確認待ち店舗を取り出してクリアする（invoke_agent から呼ぶ）"""
    shop = _SESSION_SHOP_READY.pop(session_id, None)
    print(f"[DEBUG _pop_shop_ready] session_id={session_id} result={shop.get('name') if shop else None}", flush=True)
    return shop


def _set_shop_ready(session_id: str, shop: Dict[str, Any]) -> None:
    """確認待ち店舗を直接設定する（候補番号選択時）"""
    _SESSION_SHOP_READY[session_id] = shop
    print(f"[DEBUG _set_shop_ready] session_id={session_id} shop={shop.get('name')}", flush=True)


def _clear_shop_ready(session_id: str) -> None:
    _SESSION_SHOP_READY.pop(session_id, None)


def _get_recommendations(session_id: str) -> List[Dict[str, Any]]:
    return list(_SESSION_RECOMMENDATIONS.get(session_id, []))


# ========== Preferences (AgentCore Memory) ==========

def _add_preference(session_id: str, genre: str, name: str) -> None:
    """確定した店舗のジャンル・店名をセッション嗜好履歴に追記する。
    インメモリ（即時反映）+ AgentCore Memory（永続化）の両方に保存する。
    """
    prefs = _SESSION_PREFERENCES.setdefault(session_id, [])
    if genre and f"ジャンル:{genre}" not in prefs:
        prefs.append(f"ジャンル:{genre}")
    if name:
        entry = f"訪問済:{name}"
        if entry not in prefs:
            prefs.append(entry)
    # 直近30件のみ保持
    _SESSION_PREFERENCES[session_id] = prefs[-30:]

    # AgentCore Memory に永続化（コールドスタート後も記憶を維持）
    try:
        from agentcore_memory import record_restaurant_visit
        record_restaurant_visit(actor_id=session_id, shop_name=name, genre=genre)
    except Exception:
        pass  # 永続化失敗はサイレントに無視（インメモリは更新済み）


def _get_preference_text(session_id: str) -> str:
    """プロンプトに埋め込む嗜好情報テキストを返す。
    インメモリ（セッション内）+ AgentCore Memory（長期記憶）をマージして返す。
    """
    lines: list = []

    # 1. インメモリ嗜好（セッション内で即時反映）
    prefs = _SESSION_PREFERENCES.get(session_id, [])
    if prefs:
        genres = [p[len("ジャンル:"):] for p in prefs if p.startswith("ジャンル:")]
        visited = [p[len("訪問済:"):] for p in prefs if p.startswith("訪問済:")]
        lines.append("[このセッションでの好み]")
        if genres:
            lines.append(f"確定済みジャンル（新しい順）: {', '.join(reversed(genres[-5:]))}")
        if visited:
            lines.append(f"訪問済みお店（再提案は避ける）: {', '.join(reversed(visited[-10:]))}")

    # 2. AgentCore Memory（長期記憶：コールドスタート後も維持）
    try:
        from agentcore_memory import retrieve_user_preferences
        mem_text = retrieve_user_preferences(session_id)
        if mem_text:
            if lines:
                lines.append("")
            lines.append(mem_text)
    except Exception:
        pass  # 取得失敗はサイレントに無視（インメモリ情報のみで続行）

    return "\n".join(lines)


# ========== Session helpers ==========

def _session_id_from(payload: Dict[str, Any], context: Any) -> str:
    # Lambdaが payload["session_id"] を入れてくれてる前提（最優先）
    sid = (payload.get("session_id") or "").strip()
    if sid:
        return sid

    # 予備：contextに何か入ってる場合
    for attr in ("session_id", "runtimeSessionId", "runtime_session_id"):
        v = getattr(context, attr, None)
        if isinstance(v, str) and v.strip():
            return v.strip()

    return "unknown-session"


def _parse_location_command(prompt: str) -> Optional[Tuple[float, float, str, str]]:
    if not prompt.startswith(_LOCATION_CMD_PREFIX):
        return None

    body = prompt[len(_LOCATION_CMD_PREFIX):].strip()
    if not body:
        return None

    # 新形式: __set_location__ {"lat": ..., "lng": ..., "title": "...", "address": "..."}
    if body.startswith("{"):
        try:
            obj = json.loads(body)
            lat = float(obj["lat"])
            lng = float(obj["lng"])
            title = str(obj.get("title") or "")
            address = str(obj.get("address") or "")
            return lat, lng, title, address
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    # 旧形式: __set_location__ lat=... lng=... title=... address=...
    mlat = re.search(r"lat=([0-9\.\-]+)", body)
    mlng = re.search(r"lng=([0-9\.\-]+)", body)
    title = re.search(r"title=(.*?)(?:\saddress=|$)", body)
    address = re.search(r"address=(.*)$", body)

    if not (mlat and mlng):
        return None

    try:
        return (
            float(mlat.group(1)),
            float(mlng.group(1)),
            title.group(1).strip() if title else "",
            address.group(1).strip() if address else "",
        )
    except ValueError:
        return None
