"""Tavily Web検索"""
import os
import re
from typing import Any, Dict

from http_utils import _http_post_json, _norm_query

def _web_search(query: str, max_results: int = 5, api_key: str = "") -> str:
    if not api_key:
        return "Tavily APIキーが取得できませんでした。Identity の設定を確認してください。"

    q = _norm_query(query)
    resp = _http_post_json(
        "https://api.tavily.com/search",
        {
            "api_key": api_key,
            "query": q,
            "search_depth": "basic",
            "max_results": max(1, min(10, int(max_results))),
            "include_answer": False,
            "include_raw_content": False,
        },
        timeout=20,
    )

    results = resp.get("results", []) or []
    lines = []
    for r in results[: max(1, min(10, int(max_results)))]:
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
