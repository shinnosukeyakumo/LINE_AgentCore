"""
hotpepper_gateway_tool.py
=========================
AgentCore Gateway に登録する「飲食店検索」Lambda ツール。

【認証フロー】
  Gateway の JWT オーソライザーが呼び出し元の認証を行う。
  Lambda は HOTPEPPER_API_KEY 環境変数から API キーを取得して Hotpepper API を呼び出す。

【呼び出しフロー】
  agent.py (MCPClient, Bearer トークン)
    → AgentCore Gateway (JWT 認証)
      → この Lambda
        → Hotpepper API を呼び出す
"""
import json
import logging
import os
import urllib.parse
import urllib.request
from typing import Any, Dict, List

logger = logging.getLogger()
logger.setLevel(logging.INFO)

HOTPEPPER_API_KEY = os.environ.get("HOTPEPPER_API_KEY", "").strip()


def _hotpepper_search(
    lat: float,
    lng: float,
    keyword: str,
    range_level: int,
    count: int,
) -> List[Dict[str, Any]]:
    """Hotpepper API で飲食店を検索する"""
    if not HOTPEPPER_API_KEY:
        logger.error("HOTPEPPER_API_KEY が未設定です")
        return []

    params = {
        "key": HOTPEPPER_API_KEY,
        "lat": str(lat),
        "lng": str(lng),
        "range": str(max(1, min(5, int(range_level)))),
        "keyword": keyword,
        "count": str(max(1, min(50, int(count)))),
        "format": "json",
        "order": "4",
    }
    url = "https://webservice.recruit.co.jp/hotpepper/gourmet/v1/?" + urllib.parse.urlencode(params, safe=",")
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.error("Hotpepper API エラー: %s", exc)
        return []
    return (((data.get("results") or {}).get("shop")) or [])


def _format_shop(shop: Dict[str, Any]) -> str:
    name = (shop.get("name") or "").strip() or "店舗名不明"
    genre = ((shop.get("genre") or {}).get("name") or "").strip()
    catch = (shop.get("catch") or "").strip()
    budget = ((shop.get("budget") or {}).get("name") or "").strip()
    open_ = (shop.get("open") or "").strip()
    url = ((shop.get("urls") or {}).get("pc") or "").strip()

    lines = [f"店名: {name}"]
    if genre:
        lines.append(f"ジャンル: {genre}")
    if catch:
        lines.append(f"ひとこと: {catch}")
    if budget:
        lines.append(f"予算: {budget}")
    if open_:
        lines.append(f"営業時間: {open_}")
    if url:
        lines.append(f"Hotpepper: {url}")
    return "\n".join(lines)


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """AgentCore Gateway から呼び出されるエントリポイント。"""
    body = event.get("body") or {}
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            body = {}
    # Gateway は MCP ツール引数をトップレベルの event として渡す
    if not body or not body.get("lat"):
        body = event

    logger.info("リクエスト body キー: %s", list(body.keys()))

    lat = float(body.get("lat", 0.0))
    lng = float(body.get("lng", 0.0))
    keyword = str(body.get("keyword", "飲食店")).strip()
    range_level = int(body.get("range_level", 2))
    count = int(body.get("count", 5))

    if not lat or not lng:
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "lat/lng が必要です"}, ensure_ascii=False),
        }

    logger.info("飲食店検索: keyword=%s lat=%s lng=%s", keyword, lat, lng)

    shops = _hotpepper_search(lat=lat, lng=lng, keyword=keyword, range_level=range_level, count=count)

    if not shops:
        result_text = f"「{keyword}」の条件に合う店舗が見つかりませんでした。"
        shop_json = ""
    else:
        result_text = f"「{keyword}」のおすすめはこちらです！\n\n{_format_shop(shops[0])}"
        shop_json = json.dumps(shops[0], ensure_ascii=False)

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "result": result_text,
            "shop_count": len(shops),
            "shop_json": shop_json,
        }, ensure_ascii=False),
    }
