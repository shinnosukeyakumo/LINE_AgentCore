import base64
import hashlib
import hmac
import json
import logging
import os
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ========= ENV =========
LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"].strip()
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"].strip()
AGENTCORE_RUNTIME_ARN = os.environ["AGENTCORE_RUNTIME_ARN"].strip()

AGENTCORE_REGION = os.environ.get("AGENTCORE_REGION", "us-west-2").strip()
LOADING_SECONDS = int(os.environ.get("LOADING_SECONDS", "60"))
MIN_SEND_INTERVAL = float(os.environ.get("MIN_SEND_INTERVAL", "1.0"))
FINAL_TEXT_LIMIT = int(os.environ.get("FINAL_TEXT_LIMIT", "5000"))

agentcore_client = boto3.client("bedrock-agentcore", region_name=AGENTCORE_REGION)

# agent.py と同じマーカー文字列
SHOP_CONFIRM_MARKER = "__SHOP_CONFIRM__:"

# Lambdaコンテナ内のインメモリ確認待ち店舗 { user_id -> shop_data }
# コールドスタート時にリセットされるが、同コンテナ内での連続操作には対応できる
_PENDING_SHOPS: Dict[str, Dict[str, Any]] = {}
# 全候補リスト { user_id -> [shop1, shop2, shop3] }（次の候補表示に使用）
_PENDING_CANDIDATES: Dict[str, List[Dict[str, Any]]] = {}

TOOL_STATUS_MAP = {
    "current_time": "現在時刻を確認しています...",
    "web_search": "ウェブ検索しています...",
    "websearch": "ウェブ検索しています...",
    "restaurant_search": "飲食店情報を検索しています...",
    "restaurant_location": "店舗の場所を案内しています...",
    "tavily": "ウェブ検索しています...",
    "recruit": "飲食店情報を検索しています...",
    "hotpepper": "飲食店情報を検索しています...",
    # マルチエージェント構成の委譲ツール
    "delegate_to_restaurant": "飲食店情報を検索しています...",
    "delegate_to_websearch": "ウェブ検索しています...",
}


# =========================================================
# __SHOP_CONFIRM__ マーカー解析
# =========================================================

def extract_shop_confirm_data(text: str) -> Tuple[str, Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    agent.py が末尾に付けた __SHOP_CONFIRM__:{json} 行を抽出する。
    新形式: {"current": shop_dict, "all": [shop1, shop2, shop3]}
    旧形式: shop_dict（後方互換）
    Returns: (ユーザー表示用テキスト, 現在の店舗 dict or None, 全候補リスト)
    """
    lines = text.split("\n")
    clean_lines: List[str] = []
    current_shop: Optional[Dict[str, Any]] = None
    all_candidates: List[Dict[str, Any]] = []
    for line in lines:
        if line.startswith(SHOP_CONFIRM_MARKER):
            json_str = line[len(SHOP_CONFIRM_MARKER):].strip()
            try:
                parsed = json.loads(json_str)
                if isinstance(parsed, dict) and "current" in parsed and "all" in parsed:
                    # 新形式: {current: {...}, all: [...]}
                    current_shop = parsed["current"]
                    all_candidates = parsed.get("all") or [current_shop]
                elif isinstance(parsed, dict):
                    # 旧形式: shop_dict
                    current_shop = parsed
                    all_candidates = [parsed]
            except json.JSONDecodeError:
                logger.warning(f"Failed to parse shop confirm data: {json_str[:100]}")
        else:
            clean_lines.append(line)
    return "\n".join(clean_lines).strip(), current_shop, all_candidates


def format_shop_location_for_line(shop: Dict[str, Any]) -> str:
    """次の候補の場所情報をテキスト形式で返す（AgentCore 呼び出し不要）"""
    name = (shop.get("name") or "不明").strip()
    address = (shop.get("address") or "").strip()
    lat = shop.get("lat")
    lng = shop.get("lng")
    access = (shop.get("access") or "").strip()
    open_h = (shop.get("open") or "").strip()
    close_d = (shop.get("close") or "").strip()
    hotpepper = ((shop.get("urls") or {}).get("pc") or "").strip()

    lines = [f"こちらのお店はいかがですか？", f"店名: {name}"]
    if address:
        lines.append(f"📍 {address}")
    if lat and lng:
        lines.append(f"🗺️ https://maps.google.com/?q={lat},{lng}")
    if access:
        lines.append(f"🚶 {access}")
    if open_h:
        lines.append(f"🕐 {open_h}")
    if close_d:
        lines.append(f"📅 定休日: {close_d}")
    if hotpepper:
        lines.append(f"🔗 {hotpepper}")
    return "\n".join(lines)


# =========================================================
# LINE API helpers
# =========================================================

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
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as e:
        logger.error(f"LINE API request failed: {e}")


def push_message(to: str, text: str) -> None:
    t = (text or "").strip()
    if not t:
        return
    _line_req(
        "https://api.line.me/v2/bot/message/push",
        {"to": to, "messages": [{"type": "text", "text": t}]},
    )


def push_flex_message(to: str, alt_text: str, contents: Dict[str, Any]) -> None:
    _line_req(
        "https://api.line.me/v2/bot/message/push",
        {"to": to, "messages": [{"type": "flex", "altText": alt_text, "contents": contents}]},
    )


def reply_messages(reply_token: str, messages: List[Dict[str, Any]]) -> None:
    """Reply API: 最大5件のメッセージを1回のAPIコールで送信（Push API の消費なし・無制限）"""
    if not reply_token or not messages:
        return
    _line_req(
        "https://api.line.me/v2/bot/message/reply",
        {"replyToken": reply_token, "messages": messages[:5]},
    )


def show_loading(user_id: str) -> None:
    try:
        _line_req(
            "https://api.line.me/v2/bot/chat/loading/start",
            {"chatId": user_id, "loadingSeconds": max(5, min(60, LOADING_SECONDS))},
        )
    except Exception as e:
        logger.warning(f"Loading animation failed: {e}")


# =========================================================
# Flex メッセージ構築
# =========================================================

def build_confirmation_flex(shop: Dict[str, Any], next_idx: int = 2) -> Dict[str, Any]:
    """「ここに決定」/「次の候補」/「詳細を見る」ボタン付き Flex Message を構築する
    next_idx: 次に表示する候補の番号（2=2件目, 3=3件目, 4以上=候補なし）
    """
    name = (shop.get("name") or "不明").strip()
    genre = str((shop.get("genre") or {}).get("name") or "")
    catch = (shop.get("catch") or "").strip()
    budget = str((shop.get("budget") or {}).get("name") or "")
    open_h = (shop.get("open") or "").strip()
    hotpepper_url = ((shop.get("urls") or {}).get("pc") or "").strip()

    body_contents: List[Dict[str, Any]] = [
        {
            "type": "text",
            "text": "このお店に決めますか？",
            "weight": "bold",
            "size": "sm",
            "color": "#888888",
        },
        {
            "type": "text",
            "text": name,
            "weight": "bold",
            "size": "xl",
            "color": "#1a1a1a",
            "wrap": True,
        },
    ]
    if genre:
        body_contents.append({"type": "text", "text": f"🍽️  {genre}", "size": "sm", "color": "#555555"})
    if catch:
        body_contents.append({"type": "text", "text": catch, "size": "sm", "color": "#333333", "wrap": True})
    if budget:
        body_contents.append({"type": "text", "text": f"💰  {budget}", "size": "sm", "color": "#555555"})
    if open_h:
        body_contents.append({"type": "text", "text": f"🕐  {open_h}", "size": "xs", "color": "#777777", "wrap": True})

    # 決定・次の候補ボタン（縦並びにしてフル幅確保）
    if next_idx <= 3:
        next_btn_label = f"🔄 次の候補を見る（{next_idx}件目）"
        next_btn_data = f"action=search_again&next_idx={next_idx}"
    else:
        next_btn_label = "🔄 別のお店を探す"
        next_btn_data = "action=new_search"

    footer_contents: List[Dict[str, Any]] = [
        {
            "type": "button",
            "action": {
                "type": "postback",
                "label": "✅ ここに決定",
                "data": "action=confirm_restaurant",
            },
            "style": "primary",
            "height": "sm",
        },
        {
            "type": "button",
            "action": {
                "type": "postback",
                "label": next_btn_label,
                "data": next_btn_data,
            },
            "style": "secondary",
            "height": "sm",
        },
    ]

    # Hotpepper リンクボタンを別行で追加
    if hotpepper_url:
        footer_contents.append({
            "type": "button",
            "action": {
                "type": "uri",
                "label": "🔗 Hotpepperで詳細を見る",
                "uri": hotpepper_url,
            },
            "style": "link",
            "color": "#FF8C00",
        })

    return {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "paddingAll": "16px",
            "contents": body_contents,
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "paddingAll": "8px",
            "contents": footer_contents,
        },
    }


# =========================================================
# 署名検証・ボディ解析
# =========================================================

def _verify_signature(body: bytes, signature_b64: str) -> bool:
    mac = hmac.new(LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(mac).decode("utf-8")
    return hmac.compare_digest(expected, signature_b64 or "")


def _get_header(headers: Dict[str, Any], key: str) -> Optional[str]:
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



# =========================================================
# SSE ストリーム処理
# =========================================================

def process_sse_stream(
    reply_to: str,
    user_id: str,
    response: Dict[str, Any],
    reply_token: Optional[str] = None,
    base_candidate_idx: int = 1,
) -> None:
    """
    AgentCore SSEストリームを処理し LINE へ送信する。
    reply_token が指定されている場合は Reply API（無制限）を優先使用する。
    ツール状態通知は送信せず、最終テキスト+Flexを1回のAPIコールで送る。
    """
    text_buffer = ""
    last_text_block = ""
    pending_shop_data: Optional[Tuple[Dict[str, Any], List[Dict[str, Any]]]] = None

    def _send_text(text: str) -> None:
        """テキストメッセージを送信（reply 優先、なければ push）"""
        t = (text or "").strip()
        if not t:
            return
        if reply_token:
            reply_messages(reply_token, [{"type": "text", "text": t[:FINAL_TEXT_LIMIT]}])
        else:
            push_message(reply_to, t[:FINAL_TEXT_LIMIT])

    def _send_text_and_flex(text: str, flex_contents: Dict[str, Any], flex_alt: str) -> None:
        """テキスト+Flexを1回のAPIコールで送信（reply 優先）"""
        messages: List[Dict[str, Any]] = []
        t = (text or "").strip()
        if t:
            messages.append({"type": "text", "text": t[:FINAL_TEXT_LIMIT]})
        messages.append({"type": "flex", "altText": flex_alt, "contents": flex_contents})
        if reply_token:
            reply_messages(reply_token, messages)
        else:
            # push fallback（メッセージを分割して送信）
            if t:
                push_message(reply_to, t[:FINAL_TEXT_LIMIT])
            push_flex_message(reply_to, flex_alt, flex_contents)

    try:
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
            if not isinstance(event, dict):
                continue
            inner = event.get("event")
            if not isinstance(inner, dict):
                continue

            cbd = inner.get("contentBlockDelta")
            if isinstance(cbd, dict):
                txt = (cbd.get("delta") or {}).get("text") or ""
                if txt:
                    text_buffer += txt
                continue

            cbs = inner.get("contentBlockStart")
            if isinstance(cbs, dict):
                # ツール状態通知は送信しない（push消費を避けるため）
                # ローディングアニメーションで代替済み
                tool_use = (cbs.get("start") or {}).get("toolUse") or {}
                if isinstance(tool_use, dict) and tool_use:
                    text_buffer = ""
                continue

            if "contentBlockStop" in inner:
                if text_buffer.strip():
                    block = text_buffer.strip()
                    clean_block, block_shop, block_candidates = extract_shop_confirm_data(block)
                    if block_shop:
                        pending_shop_data = (block_shop, block_candidates)
                        if clean_block:
                            last_text_block = clean_block
                    else:
                        last_text_block = block
                text_buffer = ""
                continue

    except Exception as e:
        logger.error(f"Error processing SSE stream: {e}")
        _send_text("エラーが発生しました。もう一度お試しください。")
    finally:
        try:
            response["response"].close()
        except Exception:
            pass

    if not last_text_block and not pending_shop_data:
        _send_text("応答が取得できませんでした。もう一度お試しください。")
        return

    clean_text = last_text_block
    if pending_shop_data:
        shop_data, all_candidates = pending_shop_data
    else:
        clean_text, shop_data, all_candidates = extract_shop_confirm_data(last_text_block)

    if shop_data and user_id:
        _PENDING_SHOPS[user_id] = shop_data
        _PENDING_CANDIDATES[user_id] = all_candidates if all_candidates else [shop_data]
        hotpepper_url = ((shop_data.get("urls") or {}).get("pc") or "").strip()
        if hotpepper_url and clean_text:
            clean_text = clean_text.rstrip() + f"\n\n🔗 {hotpepper_url}"
        next_idx = base_candidate_idx + 1
        _send_text_and_flex(
            clean_text,
            build_confirmation_flex(shop_data, next_idx=next_idx),
            f"「{shop_data.get('name', 'このお店')}」に決めますか？",
        )
    else:
        _send_text(last_text_block)


# =========================================================
# AgentCore 呼び出し
# =========================================================

def invoke_agentcore(runtime_session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return agentcore_client.invoke_agent_runtime(
        agentRuntimeArn=AGENTCORE_RUNTIME_ARN,
        runtimeSessionId=runtime_session_id,
        payload=body,
        qualifier="DEFAULT",
    )


# =========================================================
# ポストバック（ボタン押下）ハンドラ
# =========================================================

def handle_confirm_restaurant(user_id: str, reply_to: str, session_id: str, reply_token: str = "") -> None:
    """「ここに決定」ボタン処理"""
    shop = _PENDING_SHOPS.pop(user_id, None)
    _PENDING_CANDIDATES.pop(user_id, None)
    if not shop:
        reply_messages(reply_token, [{"type": "text", "text": "確定するお店が見つかりませんでした。\nもう一度検索してください。"}]) if reply_token else push_message(reply_to, "確定するお店が見つかりませんでした。\nもう一度検索してください。")
        return

    name = shop.get("name") or "不明"
    lat = shop.get("lat")
    lng = shop.get("lng")
    address = shop.get("address") or ""
    hotpepper = (shop.get("urls") or {}).get("pc") or ""
    access = shop.get("access") or ""
    open_h = shop.get("open") or ""
    close_d = shop.get("close") or ""

    lines = [f"✅ 「{name}」に決定しました！"]
    if address:
        lines.append(f"📍 {address}")
    if lat and lng:
        lines.append(f"🗺️ https://maps.google.com/?q={lat},{lng}")
    if access:
        lines.append(f"🚶 {access}")
    if open_h:
        lines.append(f"🕐 {open_h}")
    if close_d:
        lines.append(f"📅 定休日: {close_d}")
    if hotpepper:
        lines.append(f"🔗 {hotpepper}")

    # Reply API で送信（push 消費なし）
    if reply_token:
        reply_messages(reply_token, [{"type": "text", "text": "\n".join(lines)}])
    else:
        push_message(reply_to, "\n".join(lines))

    # AgentCore に確定通知（記憶更新のみ・レスポンスはユーザーに見せない）
    shop_json = json.dumps(shop, ensure_ascii=False)
    confirm_prompt = f"__confirm__ {shop_json}"
    try:
        resp = invoke_agentcore(session_id, {"prompt": confirm_prompt, "session_id": session_id})
        # ストリームを消費するだけ（レスポンスは送信しない）
        for _ in (resp.get("response") or []):
            pass
    except Exception as e:
        logger.error(f"Confirm notify to AgentCore failed: {e}")
    finally:
        try:
            resp["response"].close()  # type: ignore[possibly-undefined]
        except Exception:
            pass


def handle_search_again(user_id: str, reply_to: str, next_idx: int = 2, reply_token: str = "") -> None:
    """「次の候補」ボタン処理: Lambdaに保存済みの候補リストを使い AgentCore 呼び出しなしで表示する"""
    all_candidates = _PENDING_CANDIDATES.get(user_id, [])
    candidate_idx = next_idx - 1

    if 0 <= candidate_idx < len(all_candidates):
        selected = all_candidates[candidate_idx]
        _PENDING_SHOPS[user_id] = selected
        location_text = format_shop_location_for_line(selected)
        flex = build_confirmation_flex(selected, next_idx=next_idx + 1)
        alt = f"「{selected.get('name', 'このお店')}」に決めますか？"
        messages: List[Dict[str, Any]] = [
            {"type": "text", "text": location_text},
            {"type": "flex", "altText": alt, "contents": flex},
        ]
        if reply_token:
            reply_messages(reply_token, messages)
        else:
            push_message(reply_to, location_text)
            push_flex_message(reply_to, alt, flex)
    else:
        _PENDING_SHOPS.pop(user_id, None)
        _PENDING_CANDIDATES.pop(user_id, None)
        msg = "近くの候補をすべてご案内しました！\n別のキーワードで再検索しますか？\n例: 近くのイタリアン / 駅前の居酒屋"
        if reply_token:
            reply_messages(reply_token, [{"type": "text", "text": msg}])
        else:
            push_message(reply_to, msg)


def handle_new_search(user_id: str, reply_to: str, reply_token: str = "") -> None:
    """「別の店を探す」ボタン処理（候補が尽きたとき）"""
    _PENDING_SHOPS.pop(user_id, None)
    _PENDING_CANDIDATES.pop(user_id, None)
    msg = "わかりました！別のお店を検索します。\nどんなお店をお探しですか？\n例: 近くのイタリアン / 駅前の居酒屋"
    if reply_token:
        reply_messages(reply_token, [{"type": "text", "text": msg}])
    else:
        push_message(reply_to, msg)


# =========================================================
# Lambda ハンドラ
# =========================================================

def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:  # noqa: ARG001
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
        ev_type = ev.get("type")
        source = ev.get("source") or {}
        src_type = source.get("type")
        user_id = source.get("userId") or ""
        group_id = source.get("groupId")
        room_id = source.get("roomId")
        reply_to = group_id or room_id or user_id
        if not reply_to:
            continue
        session_id = reply_to
        reply_token: str = ev.get("replyToken") or ""

        # ---- ポストバックイベント（Flexボタン押下） ----
        if ev_type == "postback":
            postback_data = (ev.get("postback") or {}).get("data") or ""
            params = dict(urllib.parse.parse_qsl(postback_data))
            action = params.get("action")

            if action == "confirm_restaurant":
                handle_confirm_restaurant(user_id, reply_to, session_id, reply_token=reply_token)
            elif action == "search_again":
                next_idx = int(params.get("next_idx", "2"))
                handle_search_again(user_id, reply_to, next_idx=next_idx, reply_token=reply_token)
            elif action == "new_search":
                handle_new_search(user_id, reply_to, reply_token=reply_token)
            continue

        if ev_type != "message":
            continue

        msg = ev.get("message") or {}
        mtype = msg.get("type")

        # 1対1のみローディング表示
        if src_type == "user" and user_id:
            show_loading(user_id)

        # ---- テキストメッセージ ----
        if mtype == "text":
            text = (msg.get("text") or "").strip()
            if not text:
                continue

            try:
                resp = invoke_agentcore(session_id, {"prompt": text, "session_id": session_id})
                process_sse_stream(reply_to, user_id, resp, reply_token=reply_token)
            except Exception as e:
                logger.error(f"AgentCore invocation failed: {e}")
                err_msg = "エラーが発生しました。もう一度お試しください。"
                reply_messages(reply_token, [{"type": "text", "text": err_msg}]) if reply_token else push_message(reply_to, err_msg)
            continue

        # ---- 位置情報メッセージ ----
        if mtype == "location":
            lat = msg.get("latitude")
            lng = msg.get("longitude")
            address = msg.get("address") or ""
            title = msg.get("title") or ""
            loc_payload = json.dumps(
                {"lat": lat, "lng": lng, "title": title, "address": address},
                ensure_ascii=False,
            )
            cmd = f"__set_location__ {loc_payload}"
            try:
                resp = invoke_agentcore(session_id, {"prompt": cmd, "session_id": session_id})
                process_sse_stream(reply_to, user_id, resp, reply_token=reply_token)
            except Exception as e:
                logger.error(f"AgentCore invocation failed: {e}")
                err_msg = "エラーが発生しました。もう一度お試しください。"
                reply_messages(reply_token, [{"type": "text", "text": err_msg}]) if reply_token else push_message(reply_to, err_msg)
            continue

        msg_txt = "テキストか位置情報（＋ボタン）を送ってください。"
        reply_messages(reply_token, [{"type": "text", "text": msg_txt}]) if reply_token else push_message(reply_to, msg_txt)

    return {"statusCode": 200, "body": "OK"}
