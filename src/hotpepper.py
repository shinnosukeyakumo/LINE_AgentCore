"""Hotpepper飲食店検索・クエリ分類・フォーマット"""
import json
import logging
import os
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from http_utils import _http_get_json, _norm_query
from session_state import (
    _clear_recommendations,
    _get_recommendations,
    _set_recommendations,
)

SEARCH_DEBUG = (os.environ.get("SEARCH_DEBUG") or "0").strip().lower() in {"1", "true", "yes", "on"}

_LOG = logging.getLogger(__name__)

_EXIT_TOKENS = ("東口", "西口", "南口", "北口")
_OPPOSITE_EXIT = {"東口": "西口", "西口": "東口", "南口": "北口", "北口": "南口"}
_FOOD_TERMS = (
    "もんじゃ", "お好み焼き", "鉄板焼き", "たこ焼き", "餃子", "焼き鳥", "居酒屋",
    "ラーメン", "寿司", "焼肉", "中華", "イタリアン", "フレンチ", "和食", "定食",
    "海鮮", "そば", "うどん", "カフェ", "バー", "ビストロ", "バル", "おでん",
    "串焼き", "串カツ",
)


# ========== Debug ==========

def _debug_log(message: str, **kwargs: Any) -> None:
    if not SEARCH_DEBUG:
        return
    if kwargs:
        _LOG.info("%s | %s", message, json.dumps(kwargs, ensure_ascii=False))
    else:
        _LOG.info("%s", message)


# ========== クエリ分類 ==========

def _looks_like_greeting(q: str) -> bool:
    q = _norm_query(q)
    return q in {"こんにちは", "こんばんは", "おはよう", "はじめまして", "hello", "hi"} or q.endswith("こんにちは")


def _looks_like_shop_selection(text: str) -> bool:
    q = _norm_query(text)
    hints = ("件目", "番", "つ目", "これ", "それ", "この店", "その店", "がいい", "が良い", "にする", "決めた", "場所", "地図")
    return any(h in q for h in hints)


def _looks_like_restaurant_query(text: str) -> bool:
    q = _norm_query(text)
    if not q:
        return False

    if _looks_like_shop_selection(q):
        return False

    non_restaurant_hints = ("天気", "ニュース", "株価", "為替", "エラー", "バグ", "AWS", "コード")
    if any(h in q for h in non_restaurant_hints):
        return False

    patterns = (
        r"(飲食店|グルメ|ご飯|食事|ランチ|ディナー|居酒屋|レストラン|カフェ|喫茶|ラーメン|寿司|焼肉|焼き鳥|中華|イタリアン|フレンチ|和食|定食|海鮮|そば|うどん|バー|飲み)",
        r"(近く|この辺|このへん|周辺|付近|最寄り|駅前|東口|西口|南口|北口).*(店|飲食|ご飯|ランチ|ディナー|居酒屋|レストラン|カフェ|屋|屋さん)",
    )
    if any(re.search(pat, q) for pat in patterns):
        return True

    return bool(_extract_food_terms(q))


# ========== キーワード処理 ==========

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
    ignored = {"近く", "この辺", "このへん", "周辺", "近所", "付近", "最寄り", "ここらへん", "ここら辺", "飲食店", "お店", "店"}
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


# ========== Hotpepper APIクエリ ==========

def _hotpepper_query(lat: float, lng: float, keyword: str, range_level: int, count: int, api_key: str = "") -> List[Dict[str, Any]]:
    params = {
        "key": api_key,
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


# ========== スコアリング ==========

def _shop_search_blob(shop: Dict[str, Any]) -> str:
    return " ".join([
        (shop.get("name") or ""),
        ((shop.get("genre") or {}).get("name") or ""),
        (shop.get("catch") or ""),
        (shop.get("access") or ""),
        (shop.get("address") or ""),
    ]).lower()


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


def _prioritize_shops_by_tokens(
    shops: List[Dict[str, Any]], tokens: List[str]
) -> Tuple[List[Dict[str, Any]], bool]:
    if not shops:
        return [], False
    if not tokens:
        return shops, False

    has_direction = any(t in _EXIT_TOKENS for t in tokens)
    scored = []
    for idx, shop in enumerate(shops):
        score, matched, direction_hit, opposite_hit = _score_shop_for_tokens(shop, tokens)
        scored.append({
            "shop": shop,
            "score": score,
            "matched": matched,
            "direction_hit": direction_hit,
            "opposite_hit": opposite_hit,
            "idx": idx,
        })

    def _sorted(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return sorted(rows, key=lambda row: (row["score"], row["matched"], -row["idx"]), reverse=True)

    strict = [
        row for row in scored
        if row["matched"] > 0
        and (not has_direction or (row["direction_hit"] and not row["opposite_hit"]))
    ]
    if strict:
        return [row["shop"] for row in _sorted(strict)], True

    non_opposite = [row for row in scored if not row["opposite_hit"]]
    if non_opposite:
        return [row["shop"] for row in _sorted(non_opposite)], False

    return [row["shop"] for row in _sorted(scored)], False


# ========== フォーマット ==========

def _range_label(range_level: int) -> str:
    labels = {1: "300m", 2: "500m", 3: "1000m", 4: "2000m", 5: "3000m"}
    return labels.get(int(range_level), f"range={range_level}")


def _format_shop_line(shop: Dict[str, Any]) -> str:
    """選択確定後に出す詳細（位置情報あり）"""
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
    """候補提示時は位置情報を出さない"""
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


def _format_candidate_names(session_id: str) -> str:
    shops = _get_recommendations(session_id)
    if not shops:
        return ""
    lines = ["候補一覧:"]
    for idx, shop in enumerate(shops[:5], start=1):
        name = (shop.get("name") or "").strip() or "店舗名不明"
        lines.append(f"{idx}. {name}")
    return "\n".join(lines)


def _format_shop_results(
    shops: List[Dict[str, Any]],
    keyword: str,
    used_range: int,
    start_range: int,
    relaxed: bool,
) -> str:
    top = shops[:3]  # 最大3件表示
    lines = [f"「{keyword}」のおすすめはこちらです！"]

    if used_range > start_range:
        lines.append(
            f"（近場で見つからなかったため、{_range_label(start_range)}→{_range_label(used_range)}に範囲を広げました）"
        )

    if relaxed:
        lines.append(f"（「{keyword}」にぴったりの店が見つからなかったため、近くの候補を提示します）")

    lines.append("")

    if len(top) == 1:
        lines.append(_format_shop_candidate_line(top[0]))
    else:
        for idx, shop in enumerate(top, 1):
            lines.append(f"【{idx}件目】")
            lines.append(_format_shop_candidate_line(shop))
            if idx < len(top):
                lines.append("")
        lines.append("")
        lines.append("「2件目にする」「3件目がいい」と送ると他の候補の場所を確認できます。")
        lines.append("「ここに決定」ボタンは1件目を確定します。")

    # NOTE: __SHOP_CONFIRM__マーカーはLLMを経由させないため invoke_agent が別ブロックで送出する
    # （session_state._set_recommendations 経由で _SESSION_SHOP_READY に保存済み）

    return "\n".join(lines).strip()


# ========== 候補選択 ==========

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


# ========== メイン検索 ==========

def _search_hotpepper_with_expansion(
    session_id: str,
    lat: float,
    lng: float,
    keyword: str,
    start_range: int = 2,
    count: int = 20,
    api_key: str = "",
) -> str:
    if not api_key:
        return "Hotpepper APIキーが取得できませんでした。Identity の設定を確認してください。"

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
        specific_found = False  # 最も具体的な最初の候補キーワードで結果が得られたか

        for i, candidate in enumerate(keyword_candidates):
            queried = _hotpepper_query(lat=lat, lng=lng, keyword=candidate, range_level=range_level, count=count, api_key=api_key)
            _debug_log(
                "hotpepper_query",
                session_id=session_id,
                range_level=range_level,
                candidate=candidate,
                hit_count=len(queried),
            )
            print(f"[SEARCH] range={range_level} candidate={candidate!r} hits={len(queried)}", flush=True)

            if queried:
                for shop in queried:
                    identity = _shop_identity(shop)
                    if identity and identity not in merged:
                        merged[identity] = shop
                if i == 0:
                    specific_found = True

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
                    return_count=min(1, len(prioritized)),
                )
                print(f"[SEARCH] strict_match! range={range_level} candidate={candidate!r} shop={prioritized[0].get('name') if prioritized else '?'}", flush=True)
                _set_recommendations(session_id, prioritized, limit=3)
                return _format_shop_results(
                    prioritized,
                    keyword=cleaned_keyword,
                    used_range=range_level,
                    start_range=range_start,
                    relaxed=False,
                )
            # 非厳密マッチでは内側ループで早期リターンしない: 全候補を試してから判断する

        shops = list(merged.values())
        if not shops:
            continue

        prioritized, _ = _prioritize_shops_by_tokens(shops, tokens)
        if not fallback_shops:
            fallback_shops = prioritized or shops
            fallback_range = range_level
            _debug_log(
                "hotpepper_set_fallback",
                session_id=session_id,
                range_level=range_level,
                fallback_count=len(fallback_shops),
            )
            print(f"[SEARCH] fallback saved range={range_level} shop={fallback_shops[0].get('name') if fallback_shops else '?'}", flush=True)

        if has_direction:
            continue  # 出口指定あり: 厳密一致を求めてより広いレンジも探索

        # 出口指定なし: 具体的なキーワードで結果が得られた場合のみここで返す
        # 汎用キーワードのみで見つかった場合はより広いレンジでも検索を継続する
        if specific_found:
            _debug_log(
                "hotpepper_best_effort_match",
                session_id=session_id,
                range_level=range_level,
                specific_found=specific_found,
                merged_hit_count=len(shops),
                return_count=min(1, len(prioritized)),
            )
            print(f"[SEARCH] best_effort(specific) range={range_level} shop={prioritized[0].get('name') if prioritized else '?'}", flush=True)
            _set_recommendations(session_id, prioritized or shops, limit=3)
            return _format_shop_results(
                prioritized or shops,
                keyword=cleaned_keyword,
                used_range=range_level,
                start_range=range_start,
                relaxed=True,
            )
        print(f"[SEARCH] specific_not_found range={range_level} expanding range...", flush=True)

    if fallback_shops:
        _debug_log(
            "hotpepper_return_fallback",
            session_id=session_id,
            fallback_range=fallback_range,
            return_count=min(1, len(fallback_shops)),
        )
        _set_recommendations(session_id, fallback_shops, limit=3)
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
