import json
import os
import re
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from bedrock_agentcore import BedrockAgentCoreApp
from strands import Agent, tool
from strands.models import BedrockModel

# ========== ENV ==========
# キーはRuntimeの環境変数から取得
MODEL_ID = (os.environ.get("MODEL_ID") or "us.anthropic.claude-haiku-4-5-20251001-v1:0").strip()
TAVILY_API_KEY = (os.environ.get("TAVILY_API_KEY") or "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").strip()
RECRUIT_HOTPEPPER_API_KEY = (os.environ.get("RECRUIT_HOTPEPPER_API_KEY") or "f8bfe237f4e839dc").strip()

# ========== In-memory session state ==========
# PoC: Dynamoなしで「最後の位置情報」をセッション単位に保持
_SESSION_LOCATION: Dict[str, Dict[str, Any]] = {}
# 直近の店舗候補（位置情報は非表示のまま保持し、選択時に位置情報を返す）
_SESSION_RECOMMENDATIONS: Dict[str, List[Dict[str, Any]]] = {}

app = BedrockAgentCoreApp()
_LOCATION_CMD_PREFIX = "__set_location__"


# ========== Helpers ==========
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


def _looks_like_greeting(q: str) -> bool:
    q = _norm_query(q)
    return q in {"こんにちは", "こんばんは", "おはよう", "はじめまして", "hello", "hi"} or q.endswith("こんにちは")


def _get_location(session_id: str) -> Optional[Dict[str, Any]]:
    return _SESSION_LOCATION.get(session_id)


def _set_location(session_id: str, lat: float, lng: float, title: str = "", address: str = "") -> None:
    _SESSION_LOCATION[session_id] = {
        "lat": float(lat),
        "lng": float(lng),
        "title": _norm_query(title),
        "address": _norm_query(address),
    }


def _set_recommendations(session_id: str, shops: List[Dict[str, Any]], limit: int = 5) -> None:
    _SESSION_RECOMMENDATIONS[session_id] = list(shops[: max(1, int(limit))])


def _clear_recommendations(session_id: str) -> None:
    _SESSION_RECOMMENDATIONS.pop(session_id, None)


def _get_recommendations(session_id: str) -> List[Dict[str, Any]]:
    return list(_SESSION_RECOMMENDATIONS.get(session_id, []))


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

    body = prompt[len(_LOCATION_CMD_PREFIX) :].strip()
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


def _yield_text_event(text: str):
    """
    Lambda側が採用する Bedrock Converse Stream形式に寄せて出す。
    最低限 contentBlockDelta + contentBlockStop を出せばOK。
    """
    yield {"event": {"contentBlockDelta": {"delta": {"text": text}}}}
    yield {"event": {"contentBlockStop": {}}}
    yield {"event": {"messageStop": {}}}
    yield "[DONE]"


def _format_shop_line(shop: Dict[str, Any]) -> str:
    # 選択確定後に出す詳細（位置情報あり）
    name = (shop.get("name") or "").strip() or "店舗名不明"
    address = (shop.get("address") or "").strip()
    lat = shop.get("lat")
    lng = shop.get("lng")
    genre = ((shop.get("genre") or {}).get("name") or "").strip()
    access = (shop.get("access") or "").strip()
    open_ = (shop.get("open") or "").strip()
    close_ = (shop.get("close") or "").strip()
    url = ((shop.get("urls") or {}).get("pc") or "").strip()

    maps = ""
    if lat is not None and lng is not None:
        maps = f"https://maps.google.com/?q={lat},{lng}"

    lines = [f"店名: {name}"]
    if genre:
        lines.append(f"ジャンル: {genre}")
    if address:
        lines.append(f"住所: {address}")
    if lat is not None and lng is not None:
        lines.append(f"座標: {lat}, {lng}")
    if maps:
        lines.append(f"地図: {maps}")
    if access:
        lines.append(f"アクセス: {access}")
    if open_:
        lines.append(f"営業時間: {open_}")
    if close_:
        lines.append(f"定休日: {close_}")
    if url:
        lines.append(f"Hotpepper: {url}")

    return "\n".join(lines)


def _format_shop_candidate_line(shop: Dict[str, Any]) -> str:
    # 候補提示時は位置情報を出さない
    name = (shop.get("name") or "").strip() or "店舗名不明"
    genre = ((shop.get("genre") or {}).get("name") or "").strip()
    catch = (shop.get("catch") or "").strip()
    budget = ((shop.get("budget") or {}).get("name") or "").strip()
    open_ = (shop.get("open") or "").strip()
    close_ = (shop.get("close") or "").strip()
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
    if close_:
        lines.append(f"定休日: {close_}")
    if url:
        lines.append(f"Hotpepper: {url}")
    return "\n".join(lines)


def _format_selected_shop_location(shop: Dict[str, Any]) -> str:
    return "\n".join(["このお店ですね。場所はこちらです。", "", _format_shop_line(shop)]).strip()


def _sanitize_food_keyword(text: str) -> str:
    q = _norm_query(text).replace("　", " ")
    q = re.sub(r"[?？!！。、「」『』（）()]+", " ", q)

    strip_patterns = [
        r"(近く|この辺|このへん|周辺|近所|付近|最寄り|ここらへん|ここら辺)の?",
        r"(おすすめ|オススメ)",
        r"(を)?(探して|探す|調べて|教えて|知りたい)",
        r"(ありますか|ある|ない)$",
        r"^(飲食店|お店|店)$",
    ]
    for pat in strip_patterns:
        q = re.sub(pat, " ", q)

    q = re.sub(r"\s+", " ", q).strip(" 、")
    return q or "飲食店"


def _keyword_tokens(keyword: str) -> List[str]:
    ignored = {
        "近く",
        "この辺",
        "このへん",
        "周辺",
        "近所",
        "付近",
        "最寄り",
        "ここらへん",
        "ここら辺",
        "飲食店",
        "お店",
        "店",
    }
    tokens = []
    for token in _norm_query(keyword).split(" "):
        t = token.strip()
        if t and t not in ignored:
            tokens.append(t)
    return tokens


def _hotpepper_query(lat: float, lng: float, keyword: str, range_level: int, count: int) -> List[Dict[str, Any]]:
    params = {
        "key": RECRUIT_HOTPEPPER_API_KEY,
        "lat": str(lat),
        "lng": str(lng),
        "range": str(max(1, min(5, int(range_level)))),
        "keyword": _norm_query(keyword),
        "count": str(max(1, min(50, int(count)))),
        "format": "json",
        "order": "4",  # おすすめ順
    }
    url = "https://webservice.recruit.co.jp/hotpepper/gourmet/v1/?" + urllib.parse.urlencode(params, safe=",")
    data = _http_get_json(url, timeout=10)
    return (((data.get("results") or {}).get("shop")) or [])


def _filter_shops_by_tokens(shops: List[Dict[str, Any]], tokens: List[str]) -> List[Dict[str, Any]]:
    if not tokens:
        return shops

    out = []
    for s in shops:
        hay = " ".join(
            [
                (s.get("name") or ""),
                ((s.get("genre") or {}).get("name") or ""),
                (s.get("catch") or ""),
                (s.get("access") or ""),
                (s.get("address") or ""),
            ]
        ).lower()

        if all(t.lower() in hay for t in tokens):
            out.append(s)

    return out


def _range_label(range_level: int) -> str:
    labels = {1: "300m", 2: "500m", 3: "1000m", 4: "2000m", 5: "3000m"}
    return labels.get(int(range_level), f"range={range_level}")


def _format_shop_results(
    shops: List[Dict[str, Any]],
    keyword: str,
    used_range: int,
    start_range: int,
    relaxed: bool,
) -> str:
    result_count = min(5, len(shops))
    lines = [f"検索キーワード: {keyword}", f"候補を{result_count}件見つけました。"]

    if used_range > start_range:
        lines.append(
            f"近場で見つからなかったため、検索範囲を{_range_label(start_range)}から{_range_label(used_range)}まで広げました。"
        )

    if relaxed:
        lines.append("条件に完全一致する店舗が少ないため、近い候補も含めています。")

    lines.append("この段階では位置情報は表示しません。")
    lines.append("気になるお店の番号か店名を返信してください。例: 2件目がいい / ○○がいい")
    lines.append("")

    for i, shop in enumerate(shops[:result_count], start=1):
        lines.append(f"{i}件目")
        lines.append(_format_shop_candidate_line(shop))
        lines.append("")

    return "\n".join(lines).strip()


def _search_hotpepper_with_expansion(
    session_id: str,
    lat: float,
    lng: float,
    keyword: str,
    start_range: int = 2,
    count: int = 10,
) -> str:
    if not RECRUIT_HOTPEPPER_API_KEY:
        return "RECRUIT_HOTPEPPER_API_KEY が未設定です。"

    cleaned_keyword = _sanitize_food_keyword(keyword)
    range_start = max(1, min(5, int(start_range)))
    count = max(1, min(50, int(count)))

    tokens = _keyword_tokens(cleaned_keyword)
    fallback_shops: List[Dict[str, Any]] = []
    fallback_range: Optional[int] = None

    for range_level in range(range_start, 6):
        shops = _hotpepper_query(
            lat=lat,
            lng=lng,
            keyword=cleaned_keyword,
            range_level=range_level,
            count=count,
        )
        if not shops:
            continue

        if not fallback_shops:
            fallback_shops = shops
            fallback_range = range_level

        matched = _filter_shops_by_tokens(shops, tokens)
        if matched:
            _set_recommendations(session_id, matched, limit=5)
            return _format_shop_results(
                matched,
                keyword=cleaned_keyword,
                used_range=range_level,
                start_range=range_start,
                relaxed=False,
            )

    if fallback_shops:
        _set_recommendations(session_id, fallback_shops, limit=5)
        return _format_shop_results(
            fallback_shops,
            keyword=cleaned_keyword,
            used_range=fallback_range or range_start,
            start_range=range_start,
            relaxed=True,
        )

    _clear_recommendations(session_id)
    return "条件に合う店舗が見つかりませんでした。キーワードを変えるか、別の場所でお試しください。"


def _looks_like_shop_selection(text: str) -> bool:
    q = _norm_query(text)
    hints = (
        "件目",
        "番",
        "つ目",
        "これ",
        "それ",
        "この店",
        "その店",
        "がいい",
        "が良い",
        "にする",
        "決めた",
        "場所",
        "地図",
    )
    return any(h in q for h in hints)


def _format_candidate_names(session_id: str) -> str:
    shops = _get_recommendations(session_id)
    if not shops:
        return ""

    lines = ["候補一覧:"]
    for idx, shop in enumerate(shops[:5], start=1):
        name = (shop.get("name") or "").strip() or "店舗名不明"
        lines.append(f"{idx}. {name}")
    return "\n".join(lines)


def _select_shop_from_recommendations(session_id: str, choice: str) -> Optional[Dict[str, Any]]:
    shops = _get_recommendations(session_id)
    if not shops:
        return None

    q = _norm_query(choice)

    # 店名一致は最優先
    for shop in shops:
        name = (shop.get("name") or "").strip()
        if name and name in q:
            return shop

    if not _looks_like_shop_selection(q):
        return None

    m = re.search(r"([1-9][0-9]?)\s*(?:件目|番|つ目)?", q)
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(shops):
            return shops[idx]

    # 「これがいい」など曖昧指定のみ先頭候補を採用
    if any(h in q for h in ("これ", "それ", "この店", "その店")) or any(h in q for h in ("がいい", "にする", "決めた")):
        return shops[0]

    return None


def _web_search(query: str, max_results: int = 5) -> str:
    if not TAVILY_API_KEY:
        return "TAVILY_API_KEY が未設定です。"

    q = _norm_query(query)
    resp = _http_post_json(
        "https://api.tavily.com/search",
        {
            "api_key": TAVILY_API_KEY,
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


def _build_system_prompt(session_id: str) -> str:
    loc = _get_location(session_id)
    if loc:
        loc_text = (
            f"保存済み位置情報: lat={loc.get('lat')}, lng={loc.get('lng')}, "
            f"title={loc.get('title') or '-'}, address={loc.get('address') or '-'}"
        )
    else:
        loc_text = "保存済み位置情報: 未設定"

    return f"""あなたはLINEで動く飲食店サポートAIです。
利用できるツールは3つです。
1) restaurant_search: 保存済み位置情報を基準にHotpepperで飲食店を探す
2) restaurant_location: 直近の候補から、ユーザーが選んだ店舗の位置情報を返す
3) websearch: 一般Web検索

重要なルール:
・「近く」「この辺」「周辺」などは、保存済み位置情報の周辺を意味します。
・飲食店検索時、ユーザー文をそのままAPIに渡さず、料理ジャンル・業態・条件に要約したキーワードで restaurant_search を使ってください。
・飲食店の問い合わせでは、まず restaurant_search を優先し、補足情報が必要なときだけ websearch を併用してください。
・restaurant_search の返答では、複数候補を提示し、まだ位置情報（住所/座標/地図リンク）は出さないでください。
・ユーザーが候補を選んだら restaurant_location を使って、その店舗の位置情報を返してください。
・挨拶や雑談ではツールを使わず、そのまま自然に返答してください。
・位置情報が未設定で近く検索が必要な場合は、LINEの＋ボタンから位置情報共有を依頼してください。
・この会話で分かった好み（料理ジャンル、予算、雰囲気、利用シーン）を反映し、提案をパーソナライズしてください。
・出力はLINE向けに簡潔な日本語。Markdown記法は使わないでください。

{loc_text}
"""


def _build_agent(session_id: str) -> Agent:
    @tool
    def websearch(query: str, max_results: int = 5) -> str:
        """
        Web検索を行い、要点を返します。評判や補足情報の確認に使います。
        """
        return _web_search(query=query, max_results=max_results)

    @tool
    def restaurant_search(keyword: str, range_level: int = 2, count: int = 10) -> str:
        """
        保存済み位置情報を基準に、Hotpepperで飲食店を検索します。
        range_level: 1=300m,2=500m,3=1000m,4=2000m,5=3000m
        条件一致が見つからない場合は、範囲を段階的に広げます。
        """
        loc = _get_location(session_id)
        if not loc:
            return "位置情報が未設定です。LINEの＋ボタンから位置情報を送ってください。"

        return _search_hotpepper_with_expansion(
            session_id=session_id,
            lat=float(loc["lat"]),
            lng=float(loc["lng"]),
            keyword=keyword,
            start_range=range_level,
            count=count,
        )

    @tool
    def restaurant_location(choice: str) -> str:
        """
        直近で提示した候補から、ユーザーが選んだ店舗の位置情報を返します。
        choice: 例「2件目」「店名」「これがいい」
        """
        selected = _select_shop_from_recommendations(session_id=session_id, choice=choice)
        if selected:
            return _format_selected_shop_location(selected)

        names = _format_candidate_names(session_id)
        if names:
            return (
                "候補から特定できませんでした。番号か店名で指定してください。\n"
                "例: 2件目がいい / ○○がいい\n\n"
                f"{names}"
            )
        return "まだ候補を提示していません。先に「近くのイタリアン」などと送ってください。"

    model = BedrockModel(model_id=MODEL_ID) if MODEL_ID else BedrockModel(model_id="anthropic.claude-3-5-sonnet-20240620-v1:0")
    return Agent(
        model=model,
        system_prompt=_build_system_prompt(session_id),
        tools=[restaurant_search, restaurant_location, websearch],
    )


def _deterministic_response(prompt: str, session_id: str) -> Optional[str]:
    q = _norm_query(prompt)

    # 位置情報セット用特殊コマンド（Lambdaから送る）
    if q.startswith(_LOCATION_CMD_PREFIX):
        parsed = _parse_location_command(q)
        if parsed is None:
            return "位置情報の形式が不正でした。もう一度、LINEの＋ボタンから位置情報を送ってください。"

        lat, lng, title, address = parsed
        _set_location(session_id, lat, lng, title, address)
        _clear_recommendations(session_id)

        loc = _get_location(session_id) or {}
        msg = [
            "位置情報を保存しました。",
            f"座標: {loc.get('lat')}, {loc.get('lng')}",
        ]
        if loc.get("address"):
            msg.append(f"住所: {loc.get('address')}")
        msg.append("例: 近くのラーメン屋 / この辺の居酒屋")
        return "\n".join(msg)

    selected = _select_shop_from_recommendations(session_id=session_id, choice=q)
    if selected:
        return _format_selected_shop_location(selected)

    if _looks_like_shop_selection(q) and _get_recommendations(session_id):
        names = _format_candidate_names(session_id)
        return (
            "どのお店か特定できませんでした。番号か店名で指定してください。\n"
            "例: 2件目がいい / ○○がいい\n\n"
            f"{names}"
        ).strip()

    # 挨拶は検索しない
    if _looks_like_greeting(q):
        return "こんにちは。何を探しますか？ 例: 近くの家系ラーメン / この辺の居酒屋"

    return None


# ========== AgentCore entrypoint ==========
@app.entrypoint
async def invoke_agent(payload, context):
    prompt = (payload.get("prompt") or "").strip()
    session_id = _session_id_from(payload, context)

    # MODEL_IDが完全に空の場合は案内（importで落とさない）
    if not MODEL_ID:
        for ev in _yield_text_event("MODEL_ID が未設定です。AgentCore Runtime の環境変数 MODEL_ID を設定してください。"):
            yield ev
        return

    # 決定論ハンドリング
    det = _deterministic_response(prompt, session_id)
    if det is not None:
        for ev in _yield_text_event(det):
            yield ev
        return

    # LLMへ委譲（検索する/しない含めて判断）
    agent = _build_agent(session_id=session_id)

    try:
        async for event in agent.stream_async(prompt):
            # Bedrock Converse Stream形式(dict)が流れてくる想定
            # Lambda側は dict の {"event": {...}} だけ採用する
            yield event
    except Exception:
        # Runtime内で例外が起きても 502 にならないよう、メッセージで返す
        for ev in _yield_text_event("Runtime内部でエラーが発生しました。もう一度お試しください。"):
            yield ev


if __name__ == "__main__":
    app.run()
