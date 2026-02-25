import json
import logging
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
TAVILY_API_KEY = (os.environ.get("TAVILY_API_KEY")).strip()
RECRUIT_HOTPEPPER_API_KEY = (os.environ.get("RECRUIT_HOTPEPPER_API_KEY")).strip()
SEARCH_DEBUG = (os.environ.get("SEARCH_DEBUG") or "0").strip().lower() in {"1", "true", "yes", "on"}

# ========== In-memory session state ==========
# PoC: Dynamoなしで「最後の位置情報」をセッション単位に保持
_SESSION_LOCATION: Dict[str, Dict[str, Any]] = {}
# 直近の店舗候補（位置情報は非表示のまま保持し、選択時に位置情報を返す）
_SESSION_RECOMMENDATIONS: Dict[str, List[Dict[str, Any]]] = {}
_LOG = logging.getLogger(__name__)

app = BedrockAgentCoreApp()
_LOCATION_CMD_PREFIX = "__set_location__"
_EXIT_TOKENS = ("東口", "西口", "南口", "北口")
_OPPOSITE_EXIT = {"東口": "西口", "西口": "東口", "南口": "北口", "北口": "南口"}
_FOOD_TERMS = (
    "もんじゃ",
    "お好み焼き",
    "鉄板焼き",
    "たこ焼き",
    "餃子",
    "焼き鳥",
    "居酒屋",
    "ラーメン",
    "寿司",
    "焼肉",
    "中華",
    "イタリアン",
    "フレンチ",
    "和食",
    "定食",
    "海鮮",
    "そば",
    "うどん",
    "カフェ",
    "バー",
    "ビストロ",
    "バル",
    "おでん",
    "串焼き",
    "串カツ",
)


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


def _debug_log(message: str, **kwargs: Any) -> None:
    if not SEARCH_DEBUG:
        return
    if kwargs:
        _LOG.info("%s | %s", message, json.dumps(kwargs, ensure_ascii=False))
    else:
        _LOG.info("%s", message)


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
    q = re.sub(r"[?？!！。、「」『』（）()【】\[\]<>]+", " ", q)
    q = re.sub(r"(東口|西口|南口|北口)\s*側", r"\1", q)
    q = re.sub(r"(東口|西口|南口|北口)\s*(エリア|あたり|辺り|方面)", r"\1", q)

    strip_patterns = [
        r"(近く|この辺|このへん|周辺|近所|付近|最寄り|ここらへん|ここら辺)の?",
        r"(おすすめ|オススメ)",
        r"(を)?(探して|探す|調べて|教えて|知りたい)",
        r"(で|を)?(お願い|お願いします|教えてください|知りたいです)$",
        r"(ありますか|ある|ない)$",
        r"^(飲食店|お店|店)$",
    ]
    for pat in strip_patterns:
        q = re.sub(pat, " ", q)

    q = re.sub(r"\s+", " ", q).strip(" 、")
    return q or "飲食店"


def _dedupe_strs(values: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for v in values:
        t = _norm_query(v)
        if not t:
            continue
        if t in seen:
            continue
        out.append(t)
        seen.add(t)
    return out


def _extract_food_terms(text: str) -> List[str]:
    found: List[str] = []
    src = _norm_query(text)
    for term in _FOOD_TERMS:
        if term in src:
            found.append(term)
    return _dedupe_strs(found)


def _split_compound_token(token: str) -> List[str]:
    cur = token.strip()
    if not cur:
        return []

    parts = [cur]
    changed = True
    while changed:
        changed = False
        next_parts: List[str] = []
        for p in parts:
            p = p.strip()
            if not p:
                continue

            station_match = re.match(r"^(.+?駅)(.+)$", p)
            if station_match:
                next_parts.append(station_match.group(1))
                next_parts.append(station_match.group(2))
                changed = True
                continue

            split_done = False
            for marker in (*_EXIT_TOKENS, "駅前"):
                if marker in p and p != marker:
                    left, right = p.split(marker, 1)
                    if left.strip():
                        next_parts.append(left.strip())
                    next_parts.append(marker)
                    if right.strip():
                        next_parts.append(right.strip())
                    changed = True
                    split_done = True
                    break
            if split_done:
                continue

            next_parts.append(p)
        parts = next_parts

    return [p for p in parts if p]


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
    tokens: List[str] = []
    for token in _norm_query(keyword).split(" "):
        expanded = _split_compound_token(token.strip())
        if not expanded:
            expanded = [token.strip()]
        for t in expanded:
            if t and t not in ignored:
                tokens.append(t)

    uniq: List[str] = []
    seen = set()
    for t in tokens:
        if t not in seen:
            uniq.append(t)
            seen.add(t)
    return uniq


def _build_hotpepper_keyword_candidates(cleaned_keyword: str, tokens: List[str]) -> List[str]:
    candidates: List[str] = [cleaned_keyword]

    station_tokens = [t for t in tokens if t.endswith("駅")]
    direction_tokens = [t for t in tokens if t in _EXIT_TOKENS]
    ignored_generic = {"側", "エリア", "方面", "あたり", "辺り"}
    generic_tokens = [
        t for t in tokens if t not in _EXIT_TOKENS and not t.endswith("駅") and t not in ignored_generic
    ]

    food_tokens: List[str] = []
    for t in generic_tokens:
        expanded = _extract_food_terms(t)
        if expanded:
            food_tokens.extend(expanded)
        elif len(t) >= 2:
            food_tokens.append(t)
    food_tokens = _dedupe_strs(food_tokens)

    if food_tokens:
        candidates.append(" ".join(food_tokens))
        if station_tokens:
            candidates.append(" ".join([station_tokens[0], *food_tokens]))
        if station_tokens and direction_tokens:
            candidates.append(" ".join([station_tokens[0], direction_tokens[0], *food_tokens]))
        if len(food_tokens) > 1:
            candidates.append(food_tokens[0])
    else:
        if station_tokens and direction_tokens:
            candidates.append(" ".join([station_tokens[0], direction_tokens[0]]))
        if station_tokens:
            candidates.append(station_tokens[0])
        candidates.append("飲食店")

    return _dedupe_strs(candidates)[:6]


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


def _shop_search_blob(shop: Dict[str, Any]) -> str:
    return " ".join(
        [
            (shop.get("name") or ""),
            ((shop.get("genre") or {}).get("name") or ""),
            (shop.get("catch") or ""),
            (shop.get("access") or ""),
            (shop.get("address") or ""),
        ]
    ).lower()


def _shop_identity(shop: Dict[str, Any]) -> str:
    sid = str(shop.get("id") or "").strip()
    if sid:
        return sid

    name = str(shop.get("name") or "").strip()
    address = str(shop.get("address") or "").strip()
    lat = str(shop.get("lat") or "").strip()
    lng = str(shop.get("lng") or "").strip()
    return "|".join([name, address, lat, lng])


def _score_shop_for_tokens(shop: Dict[str, Any], tokens: List[str]) -> Tuple[int, int, bool, bool]:
    if not tokens:
        return 0, 0, False, False

    directions = [t for t in tokens if t in _EXIT_TOKENS]
    stations = [t for t in tokens if t.endswith("駅")]
    generic = [t for t in tokens if t not in _EXIT_TOKENS and not t.endswith("駅")]
    hay = _shop_search_blob(shop)

    score = 0
    matched = 0
    direction_hit = False
    opposite_hit = False

    for d in directions:
        if d.lower() in hay:
            score += 8
            matched += 1
            direction_hit = True
        else:
            score -= 1

        opposite = _OPPOSITE_EXIT.get(d)
        if opposite and opposite.lower() in hay:
            score -= 8
            opposite_hit = True

    for s in stations:
        if s.lower() in hay:
            score += 5
            matched += 1

    for t in generic:
        if t.lower() in hay:
            score += 2
            matched += 1

    return score, matched, direction_hit, opposite_hit


def _prioritize_shops_by_tokens(shops: List[Dict[str, Any]], tokens: List[str]) -> Tuple[List[Dict[str, Any]], bool]:
    if not shops:
        return [], False

    if not tokens:
        return shops, False

    has_direction = any(t in _EXIT_TOKENS for t in tokens)
    scored = []
    for idx, shop in enumerate(shops):
        score, matched, direction_hit, opposite_hit = _score_shop_for_tokens(shop, tokens)
        scored.append(
            {
                "shop": shop,
                "score": score,
                "matched": matched,
                "direction_hit": direction_hit,
                "opposite_hit": opposite_hit,
                "idx": idx,
            }
        )

    def _sorted(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return sorted(rows, key=lambda row: (row["score"], row["matched"], -row["idx"]), reverse=True)

    strict = [
        row
        for row in scored
        if row["matched"] > 0
        and (not has_direction or (row["direction_hit"] and not row["opposite_hit"]))
    ]
    if strict:
        return [row["shop"] for row in _sorted(strict)], True

    # 出口指定がある場合は反対出口を含む候補を除外して返す
    non_opposite = [row for row in scored if not row["opposite_hit"]]
    if non_opposite:
        return [row["shop"] for row in _sorted(non_opposite)], False

    return [row["shop"] for row in _sorted(scored)], False


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
    count: int = 20,
) -> str:
    if not RECRUIT_HOTPEPPER_API_KEY:
        return "RECRUIT_HOTPEPPER_API_KEY が未設定です。"

    cleaned_keyword = _sanitize_food_keyword(keyword)
    range_start = max(1, min(5, int(start_range)))
    count = max(1, min(50, int(count)))

    tokens = _keyword_tokens(cleaned_keyword)
    keyword_candidates = _build_hotpepper_keyword_candidates(cleaned_keyword, tokens)
    _debug_log(
        "hotpepper_search_start",
        session_id=session_id,
        original_keyword=keyword,
        cleaned_keyword=cleaned_keyword,
        tokens=tokens,
        keyword_candidates=keyword_candidates,
        range_start=range_start,
        count=count,
    )

    has_direction = any(t in _EXIT_TOKENS for t in tokens)
    fallback_shops: List[Dict[str, Any]] = []
    fallback_range: Optional[int] = None

    for range_level in range(range_start, 6):
        merged: Dict[str, Dict[str, Any]] = {}
        for candidate in keyword_candidates:
            queried = _hotpepper_query(
                lat=lat,
                lng=lng,
                keyword=candidate,
                range_level=range_level,
                count=count,
            )
            _debug_log(
                "hotpepper_query",
                session_id=session_id,
                range_level=range_level,
                candidate=candidate,
                hit_count=len(queried),
            )

            if queried:
                for shop in queried:
                    identity = _shop_identity(shop)
                    if identity and identity not in merged:
                        merged[identity] = shop

            shops = list(merged.values())
            if not shops:
                continue

            prioritized, strict_match = _prioritize_shops_by_tokens(shops, tokens)
            if strict_match:
                _debug_log(
                    "hotpepper_strict_match",
                    session_id=session_id,
                    range_level=range_level,
                    candidate=candidate,
                    merged_hit_count=len(shops),
                    return_count=min(5, len(prioritized)),
                )
                _set_recommendations(session_id, prioritized, limit=5)
                return _format_shop_results(
                    prioritized,
                    keyword=cleaned_keyword,
                    used_range=range_level,
                    start_range=range_start,
                    relaxed=False,
                )

            # 出口指定がない場合は最初に得られた有効候補で返す
            if not has_direction and prioritized:
                _debug_log(
                    "hotpepper_best_effort_match",
                    session_id=session_id,
                    range_level=range_level,
                    candidate=candidate,
                    merged_hit_count=len(shops),
                    return_count=min(5, len(prioritized)),
                )
                _set_recommendations(session_id, prioritized, limit=5)
                return _format_shop_results(
                    prioritized,
                    keyword=cleaned_keyword,
                    used_range=range_level,
                    start_range=range_start,
                    relaxed=False,
                )

        shops = list(merged.values())
        if not shops:
            continue

        if not fallback_shops:
            ranked_default, _ = _prioritize_shops_by_tokens(shops, tokens)
            fallback_shops = ranked_default or shops
            fallback_range = range_level
            _debug_log(
                "hotpepper_set_fallback",
                session_id=session_id,
                range_level=range_level,
                fallback_count=len(fallback_shops),
            )

        # 出口指定がある場合は厳密一致を優先するため、後続レンジでも探索を続ける
        if has_direction:
            continue

    if fallback_shops:
        _debug_log(
            "hotpepper_return_fallback",
            session_id=session_id,
            fallback_range=fallback_range,
            return_count=min(5, len(fallback_shops)),
        )
        _set_recommendations(session_id, fallback_shops, limit=5)
        return _format_shop_results(
            fallback_shops,
            keyword=cleaned_keyword,
            used_range=fallback_range or range_start,
            start_range=range_start,
            relaxed=True,
        )

    _clear_recommendations(session_id)
    _debug_log("hotpepper_not_found", session_id=session_id)
    return "条件に合う店舗が見つかりませんでした。キーワードを変えるか、別の場所でお試しください。"


def _looks_like_restaurant_query(text: str) -> bool:
    q = _norm_query(text)
    if not q:
        return False

    if _looks_like_shop_selection(q):
        return False

    non_restaurant_hints = (
        "天気",
        "ニュース",
        "株価",
        "為替",
        "エラー",
        "バグ",
        "AWS",
        "コード",
    )
    if any(h in q for h in non_restaurant_hints):
        return False

    patterns = (
        r"(飲食店|グルメ|ご飯|食事|ランチ|ディナー|居酒屋|レストラン|カフェ|喫茶|ラーメン|寿司|焼肉|焼き鳥|中華|イタリアン|フレンチ|和食|定食|海鮮|そば|うどん|バー|飲み)",
        r"(近く|この辺|このへん|周辺|付近|最寄り|駅前|東口|西口|南口|北口).*(店|飲食|ご飯|ランチ|ディナー|居酒屋|レストラン|カフェ|屋|屋さん)",
    )
    if any(re.search(pat, q) for pat in patterns):
        return True

    return bool(_extract_food_terms(q))


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
・飲食店の検索やおすすめは必ず Hotpepper API（restaurant_search）を使ってください。一般Web検索をメインにしないでください。
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
    def restaurant_search(keyword: str, range_level: int = 2, count: int = 20) -> str:
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

    # 飲食店の検索・おすすめは決定論でHotpepperに寄せる
    if _looks_like_restaurant_query(q):
        loc = _get_location(session_id)
        if not loc:
            return "位置情報が未設定です。LINEの＋ボタンから位置情報を送ってください。"

        return _search_hotpepper_with_expansion(
            session_id=session_id,
            lat=float(loc["lat"]),
            lng=float(loc["lng"]),
            keyword=q,
            start_range=2,
            count=20,
        )

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
