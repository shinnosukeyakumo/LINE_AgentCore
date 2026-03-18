"""AgentCore Memory - ユーザー長期嗜好記憶（永続化）

store : create_event  で訪問確定した店舗情報を記録する
search: retrieve_memory_records でセマンティック検索して嗜好を取得する
"""
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import boto3

logger = logging.getLogger(__name__)

AGENTCORE_MEMORY_ID: str = (
    os.environ.get("AGENTCORE_MEMORY_ID")
    or os.environ.get("BEDROCK_AGENTCORE_MEMORY_ID")
    or "line_agentcore_mem-9qQz574MzM"
)
AGENTCORE_REGION: str = os.environ.get("AGENTCORE_REGION", "us-west-2")

_BOTO_CLIENT: Optional[Any] = None
_PREFERENCE_CACHE: Dict[str, Tuple[float, str]] = {}  # actor_id -> (timestamp, text)
_PREFERENCE_CACHE_TTL = 300  # 5分キャッシュ（API呼び出しを削減）


def _get_client() -> Any:
    global _BOTO_CLIENT
    if _BOTO_CLIENT is None:
        _BOTO_CLIENT = boto3.client("bedrock-agentcore", region_name=AGENTCORE_REGION)
    return _BOTO_CLIENT


def record_restaurant_visit(actor_id: str, shop_name: str, genre: str) -> None:
    """「ここに決定」確定時に店舗情報をAgentCore Memoryに永続記録する。

    AgentCore Memory の memory strategies が設定されている場合、
    イベントから嗜好・ジャンル情報が自動抽出される。
    """
    if not AGENTCORE_MEMORY_ID:
        return
    try:
        genre_str = f"（{genre}）" if genre else ""
        pref_note = f"好みのジャンルは「{genre}」です。" if genre else ""
        content = (
            f"ユーザーが「{shop_name}」{genre_str}への訪問を確定しました。{pref_note}"
        )
        _get_client().create_event(
            memoryId=AGENTCORE_MEMORY_ID,
            actorId=actor_id,
            sessionId=f"pref-{actor_id}",
            eventTimestamp=datetime.now(timezone.utc),
            payload=[
                {
                    "conversational": {
                        "content": {"text": content},
                        "role": "ASSISTANT",
                    }
                }
            ],
        )
        # 記録後にキャッシュを無効化して次回取得時に最新情報を返す
        _PREFERENCE_CACHE.pop(actor_id, None)
        logger.info(
            "AgentCore Memory: visit recorded actor=%s shop=%s genre=%s",
            actor_id, shop_name, genre,
        )
    except Exception as exc:
        logger.warning("AgentCore Memory write failed: %s", exc)


def retrieve_user_preferences(actor_id: str) -> str:
    """AgentCore Memoryからユーザーの嗜好・訪問履歴をセマンティック検索で取得する。

    結果は5分間キャッシュされてAPI呼び出しを抑制する。
    取得失敗時は空文字を返してフォールバックに委ねる。
    """
    if not AGENTCORE_MEMORY_ID:
        return ""

    # キャッシュヒット確認
    now = time.time()
    if actor_id in _PREFERENCE_CACHE:
        ts, cached_text = _PREFERENCE_CACHE[actor_id]
        if now - ts < _PREFERENCE_CACHE_TTL:
            return cached_text

    try:
        response = _get_client().retrieve_memory_records(
            memoryId=AGENTCORE_MEMORY_ID,
            namespace="/",
            searchCriteria={
                "searchQuery": "ユーザーが訪問した飲食店 確定した店舗 好みのジャンル",
                "topK": 8,
            },
        )
        records = response.get("memoryRecordSummaries", [])
        texts = [
            r.get("content", {}).get("text", "")
            for r in records
            if r.get("content", {}).get("text")
        ]
        result = ""
        if texts:
            lines = ["[AgentCore Memory - 長期記憶: 過去の訪問・好み]"]
            lines.extend(f"・{t}" for t in texts[:8])
            result = "\n".join(lines)
        _PREFERENCE_CACHE[actor_id] = (now, result)
        return result
    except Exception as exc:
        logger.warning("AgentCore Memory read failed: %s", exc)
        return ""
