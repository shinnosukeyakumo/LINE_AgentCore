"""
websearch_gateway_tool.py
=========================
AgentCore Gateway に登録する「Web検索」Lambda ツール。

【認証フロー】
  Gateway の JWT オーソライザーが呼び出し元の認証を行う。
  Lambda は TAVILY_API_KEY 環境変数から API キーを取得して Tavily API を呼び出す。

【呼び出しフロー】
  agent.py (MCPClient, Bearer トークン)
    → AgentCore Gateway (JWT 認証)
      → この Lambda
        → Tavily API を呼び出す
"""
import json
import logging
import os
import re
import urllib.request
from typing import Any, Dict, List

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "").strip()


def _tavily_search(query: str, max_results: int) -> List[Dict[str, Any]]:
    """Tavily API で Web 検索する"""
    if not TAVILY_API_KEY:
        logger.error("TAVILY_API_KEY が未設定です")
        return []

    payload = json.dumps({
        "api_key": TAVILY_API_KEY,
        "query": query,
        "search_depth": "basic",
        "max_results": max(1, min(10, int(max_results))),
        "include_answer": False,
        "include_raw_content": False,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.error("Tavily API エラー: %s", exc)
        return []
    return data.get("results", []) or []


def _format_results(results: List[Dict[str, Any]], max_results: int) -> str:
    lines = []
    for r in results[:max(1, min(10, int(max_results)))]:
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        content = re.sub(r"\s+", " ", (r.get("content") or "").strip())
        if content:
            content = content[:160] + ("…" if len(content) > 160 else "")
        parts = []
        if title:
            parts.append(f"タイトル: {title}")
        if content:
            parts.append(f"要約: {content}")
        if url:
            parts.append(f"URL: {url}")
        if parts:
            lines.append("\n".join(parts))
    return "\n\n".join(lines) if lines else "検索結果が見つかりませんでした。"


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """AgentCore Gateway から呼び出されるエントリポイント。"""
    body = event.get("body") or {}
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            body = {}
    # Gateway は MCP ツール引数をトップレベルの event として渡す
    if not body or not body.get("query"):
        body = event

    logger.info("リクエスト body キー: %s", list(body.keys()))

    query = str(body.get("query", "")).strip()
    max_results = int(body.get("max_results", 5))

    if not query:
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "query が必要です"}, ensure_ascii=False),
        }

    logger.info("Web検索: query=%s", query)

    results = _tavily_search(query=query, max_results=max_results)
    result_text = _format_results(results, max_results)

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"result": result_text, "result_count": len(results)}, ensure_ascii=False),
    }
