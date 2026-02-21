import base64
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.request
from typing import Any, Dict, Optional

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ========= ENV =========
LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"].strip()
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"].strip()
AGENTCORE_RUNTIME_ARN = os.environ["AGENTCORE_RUNTIME_ARN"].strip()

AGENTCORE_REGION = os.environ.get("AGENTCORE_REGION", "us-west-2").strip()

# ローディング（5〜60秒）: 1対1のみ有効
LOADING_SECONDS = int(os.environ.get("LOADING_SECONDS", "60"))

# ツール状態pushの最低間隔（通数節約＆レート制限回避）
MIN_SEND_INTERVAL = float(os.environ.get("MIN_SEND_INTERVAL", "1.0"))

# 最終本文の上限（LINEの1メッセージ上限対策）
FINAL_TEXT_LIMIT = int(os.environ.get("FINAL_TEXT_LIMIT", "5000"))

agentcore_client = boto3.client("bedrock-agentcore", region_name=AGENTCORE_REGION)

TOOL_STATUS_MAP = {
    "current_time": "現在時刻を確認しています...",
    "web_search": "ウェブ検索しています...",
    "websearch": "ウェブ検索しています...",
    "restaurant_search": "飲食店情報を検索しています...",
    "restaurant_location": "店舗の場所を案内しています...",
    "tavily": "ウェブ検索しています...",
    "recruit": "飲食店情報を検索しています...",
    "hotpepper": "飲食店情報を検索しています...",
    "search_documentation": "AWSドキュメントを検索しています...",
    "read_documentation": "AWSドキュメントを読んでいます...",
    "recommend": "関連情報を探しています...",
    "rss": "AWS What's New RSSを取得しています...",
    "clear_memory": "会話の記憶をクリアしました！",
}


# ========= LINE HTTP =========
def _line_req(url: str, payload: Dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def show_loading(user_id: str) -> None:
    """
    ローディング表示（1対1のみ）
    POST https://api.line.me/v2/bot/chat/loading/start
    body: { chatId: user_id, loadingSeconds: 5..60 }
    """
    try:
        _line_req(
            "https://api.line.me/v2/bot/chat/loading/start",
            {"chatId": user_id, "loadingSeconds": max(5, min(60, LOADING_SECONDS))},
        )
    except Exception as e:
        logger.warning(f"Loading animation failed: {e}")


def push_message(to: str, text: str) -> None:
    t = (text or "").strip()
    if not t:
        return
    _line_req(
        "https://api.line.me/v2/bot/message/push",
        {"to": to, "messages": [{"type": "text", "text": t}]},
    )


# ========= Signature =========
def _verify_signature(body: bytes, signature_b64: str) -> bool:
    mac = hmac.new(LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(mac).decode("utf-8")
    return hmac.compare_digest(expected, signature_b64 or "")


def _get_header(headers: Dict[str, Any], key: str) -> Optional[str]:
    # API Gateway は header key の大小が揺れるので両対応
    if not headers:
        return None
    for k, v in headers.items():
        if k.lower() == key.lower():
            return v
    return None


def _parse_body(event: Dict[str, Any]) -> bytes:
    body = event.get("body") or b""
    if isinstance(body, str):
        raw = body.encode("utf-8")
    else:
        raw = body

    if event.get("isBase64Encoded"):
        return base64.b64decode(raw)
    return raw


# ========= Tool status =========
def _tool_status_text(tool_name: str) -> str:
    tn = tool_name or "unknown"
    for key, msg in TOOL_STATUS_MAP.items():
        if key in tn:
            return msg
    return f"{tn} を実行しています..."


# ========= SSE Processing =========
def process_sse_stream(reply_to: str, response: Dict[str, Any]) -> None:
    """
    - Bedrock Converse Stream形式のSSE(JSON dict)だけ採用
    - ツール実行開始だけリアルタイムpush
    - 本文は最終ブロックだけ送信（先頭1文字問題＆通数増大を防ぐ）
    """
    text_buffer = ""
    last_text_block = ""
    last_send_time = 0.0

    def throttled_push(text: str) -> None:
        nonlocal last_send_time
        elapsed = time.time() - last_send_time
        if elapsed < MIN_SEND_INTERVAL:
            time.sleep(MIN_SEND_INTERVAL - elapsed)
        push_message(reply_to, text)
        last_send_time = time.time()

    try:
        # chunk_size は 256〜1024 くらいが安定
        for line in response["response"].iter_lines(chunk_size=256):
            if not line:
                continue

            line_str = line.decode("utf-8", errors="replace")
            if not line_str.startswith("data: "):
                continue

            data_str = line_str[6:].strip()
            if data_str == "[DONE]":
                break

            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            # パターンB（文字列/配列など）は無視
            if not isinstance(event, dict):
                continue

            inner = event.get("event")
            if not isinstance(inner, dict):
                continue

            # text delta
            cbd = inner.get("contentBlockDelta")
            if isinstance(cbd, dict):
                delta = cbd.get("delta") or {}
                txt = (delta.get("text") or "")
                if txt:
                    text_buffer += txt
                continue

            # tool start
            cbs = inner.get("contentBlockStart")
            if isinstance(cbs, dict):
                start = cbs.get("start") or {}
                tool_use = start.get("toolUse") or {}
                if isinstance(tool_use, dict) and tool_use:
                    # ツール前の途中テキストは捨てる
                    text_buffer = ""
                    tool_name = tool_use.get("name", "unknown")
                    throttled_push(_tool_status_text(tool_name))
                continue

            # block stop
            if "contentBlockStop" in inner:
                if text_buffer.strip():
                    last_text_block = text_buffer.strip()
                text_buffer = ""
                continue

    except Exception as e:
        logger.error(f"Error processing SSE stream: {e}")
        push_message(reply_to, "エラーが発生しました。もう一度お試しください。")
    finally:
        try:
            response["response"].close()
        except Exception:
            pass

    if last_text_block:
        push_message(reply_to, last_text_block[:FINAL_TEXT_LIMIT])
    else:
        push_message(reply_to, "応答が取得できませんでした。もう一度お試しください。")


# ========= AgentCore invoke =========
def invoke_agentcore(runtime_session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return agentcore_client.invoke_agent_runtime(
        agentRuntimeArn=AGENTCORE_RUNTIME_ARN,
        runtimeSessionId=runtime_session_id,
        payload=body,
        qualifier="DEFAULT",
        # accept は SDK 側で良しなにされるが、環境によっては明示したい場合がある
        # accept="text/event-stream",
        # contentType="application/json",
    )


def lambda_handler(event: Dict[str, Any], context: Any):
    body_bytes = _parse_body(event)
    headers = event.get("headers") or {}
    sig = _get_header(headers, "x-line-signature") or ""

    if not sig or not _verify_signature(body_bytes, sig):
        logger.error("Invalid LINE signature")
        return {"statusCode": 400, "body": "Invalid signature"}

    payload = json.loads(body_bytes.decode("utf-8", errors="replace"))
    events = payload.get("events") or []
    if not events:
        return {"statusCode": 200, "body": "OK"}

    for ev in events:
        if ev.get("type") != "message":
            continue

        source = ev.get("source") or {}
        src_type = source.get("type")
        user_id = source.get("userId")
        group_id = source.get("groupId")
        room_id = source.get("roomId")

        # 送信先（push）
        reply_to = group_id or room_id or user_id
        if not reply_to:
            continue

        # session_id（チャット単位に寄せる）
        session_id = reply_to

        msg = ev.get("message") or {}
        mtype = msg.get("type")

        # 1対1ならローディング（グループ/ルームは不可）
        if src_type == "user" and user_id:
            show_loading(user_id)

        # テキスト
        if mtype == "text":
            text = (msg.get("text") or "").strip()
            if not text:
                continue

            try:
                resp = invoke_agentcore(
                    session_id,
                    {"prompt": text, "session_id": session_id},
                )
                process_sse_stream(reply_to, resp)
            except Exception as e:
                logger.error(f"AgentCore invocation failed: {e}")
                push_message(reply_to, "エラーが発生しました。もう一度お試しください。")
            continue

        # 位置情報（＋ボタン）
        if mtype == "location":
            lat = msg.get("latitude")
            lng = msg.get("longitude")
            address = msg.get("address") or ""
            title = msg.get("title") or ""

            # AgentCore側で __set_location__ + JSON を解釈する前提
            loc_payload = json.dumps(
                {
                    "lat": lat,
                    "lng": lng,
                    "title": title,
                    "address": address,
                },
                ensure_ascii=False,
            )
            cmd = f"__set_location__ {loc_payload}"

            try:
                resp = invoke_agentcore(
                    session_id,
                    {"prompt": cmd, "session_id": session_id},
                )
                process_sse_stream(reply_to, resp)
            except Exception as e:
                logger.error(f"AgentCore invocation failed: {e}")
                push_message(reply_to, "エラーが発生しました。もう一度お試しください。")
            continue

        # その他
        push_message(reply_to, "テキストか位置情報（＋ボタン）を送ってください。")

    return {"statusCode": 200, "body": "OK"}
