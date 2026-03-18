"""
map_handler.py
==============
ユーザーのパーソナライズ飲食店マップを提供するLambda関数。

エンドポイント例: GET /map?user_id=U1234567890abcdef

DynamoDBの line_food_map テーブルからユーザーの訪問履歴を取得し、
Leaflet.js を使ったインタラクティブマップHTMLを返す。

レビュー編集エンドポイント:
  POST /map/review
  Body: { user_id, visit_id, rating, review }
"""

import json
import logging
import os
import urllib.parse
from typing import Any, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AGENTCORE_REGION = os.environ.get("AGENTCORE_REGION", "us-west-2").strip()
FOOD_MAP_TABLE = os.environ.get("FOOD_MAP_TABLE", "line_food_map").strip()

dynamodb = boto3.resource("dynamodb", region_name=AGENTCORE_REGION)


# =========================================================
# DynamoDB 操作
# =========================================================

def get_user_restaurants(user_id: str) -> List[Dict[str, Any]]:
    try:
        table = dynamodb.Table(FOOD_MAP_TABLE)
        resp = table.query(KeyConditionExpression=Key("user_id").eq(user_id))
        return resp.get("Items") or []
    except ClientError as e:
        logger.error(f"DynamoDB query error: {e}")
        return []


def update_review(user_id: str, visit_id: str, rating: Optional[int], review: Optional[str]) -> bool:
    from datetime import datetime, timezone
    try:
        table = dynamodb.Table(FOOD_MAP_TABLE)
        update_expr = "SET updated_at = :ts"
        expr_vals: Dict[str, Any] = {":ts": datetime.now(timezone.utc).isoformat()}
        if rating is not None:
            update_expr += ", rating = :rating"
            expr_vals[":rating"] = rating
        if review is not None:
            update_expr += ", review = :review"
            expr_vals[":review"] = review
        table.update_item(
            Key={"user_id": user_id, "visit_id": visit_id},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=expr_vals,
        )
        return True
    except ClientError as e:
        logger.error(f"DynamoDB update_review error: {e}")
        return False


# =========================================================
# HTML 生成
# =========================================================

def _escape_js(s: str) -> str:
    """JavaScript文字列内に埋め込む際の最低限エスケープ"""
    return (
        s.replace("\\", "\\\\")
         .replace("'", "\\'")
         .replace("\n", "\\n")
         .replace("\r", "")
         .replace("<", "\\u003c")
    )


def render_map_html(restaurants: List[Dict[str, Any]], user_id: str, map_base_url: str) -> str:
    markers_data = []
    for r in restaurants:
        lat_s = str(r.get("lat") or "")
        lng_s = str(r.get("lng") or "")
        if not lat_s or not lng_s:
            continue
        try:
            lat = float(lat_s)
            lng = float(lng_s)
        except ValueError:
            continue

        rating = r.get("rating")
        try:
            rating_int = int(rating) if rating is not None else 0
        except (ValueError, TypeError):
            rating_int = 0

        stars_filled = "★" * rating_int + "☆" * (5 - rating_int) if rating_int else "未評価"

        markers_data.append({
            "lat": lat,
            "lng": lng,
            "name": _escape_js(str(r.get("name") or "不明")),
            "genre": _escape_js(str(r.get("genre") or "")),
            "address": _escape_js(str(r.get("address") or "")),
            "stars": stars_filled,
            "rating": rating_int,
            "review": _escape_js(str(r.get("review") or "")),
            "date": str(r.get("created_at") or "")[:10],
            "url": _escape_js(str(r.get("hotpepper_url") or "")),
            "visit_id": _escape_js(str(r.get("visit_id") or "")),
            "images": [_escape_js(u) for u in (r.get("images") or [])],
        })

    if markers_data:
        center_lat = sum(m["lat"] for m in markers_data) / len(markers_data)
        center_lng = sum(m["lng"] for m in markers_data) / len(markers_data)
        zoom = 13
    else:
        center_lat, center_lng = 35.6762, 139.6503  # 東京デフォルト
        zoom = 12

    markers_json = json.dumps(markers_data, ensure_ascii=False)
    count = len(markers_data)
    encoded_user = urllib.parse.quote(user_id, safe="")
    review_api_url = f"{map_base_url}/review" if map_base_url else "/review"

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
  <title>🍽️ マイ飲食店マップ</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f5; }}
    #header {{
      position: fixed; top: 0; left: 0; right: 0; z-index: 2000;
      background: #00b900; color: white;
      padding: 12px 16px;
      display: flex; align-items: center; justify-content: space-between;
      box-shadow: 0 2px 8px rgba(0,0,0,0.2);
    }}
    #header h1 {{ font-size: 16px; font-weight: bold; }}
    #header span {{ font-size: 13px; opacity: 0.9; }}
    #map {{ position: fixed; top: 48px; left: 0; right: 0; bottom: 0; }}
    /* ポップアップ */
    .popup-wrap {{ max-width: 280px; font-size: 13px; }}
    .popup-name {{ font-weight: bold; font-size: 15px; margin-bottom: 6px; color: #1a1a1a; }}
    .popup-genre {{ color: #666; margin-bottom: 4px; }}
    .popup-stars {{ color: #f5a623; font-size: 16px; margin-bottom: 6px; }}
    .popup-review {{ color: #333; margin: 6px 0; font-style: italic; }}
    .popup-address {{ color: #555; font-size: 12px; margin-bottom: 4px; }}
    .popup-date {{ color: #aaa; font-size: 11px; margin-bottom: 8px; }}
    .popup-link {{ color: #00b900; text-decoration: none; font-weight: bold; }}
    .popup-imgs {{ display: flex; gap: 4px; flex-wrap: wrap; margin: 6px 0; }}
    .popup-imgs img {{ width: 70px; height: 70px; object-fit: cover; border-radius: 6px; }}
    .btn-review {{
      display: inline-block; margin-top: 8px;
      background: #00b900; color: white;
      border: none; border-radius: 20px;
      padding: 6px 16px; font-size: 13px; cursor: pointer;
      width: 100%;
    }}
    /* レビューモーダル */
    #modal-overlay {{
      display: none; position: fixed; inset: 0;
      background: rgba(0,0,0,0.5); z-index: 3000;
      align-items: center; justify-content: center;
    }}
    #modal-overlay.show {{ display: flex; }}
    #modal {{
      background: white; border-radius: 16px; padding: 24px;
      width: 90%; max-width: 400px; max-height: 90vh; overflow-y: auto;
    }}
    #modal h2 {{ font-size: 16px; margin-bottom: 16px; }}
    .star-row {{ display: flex; gap: 8px; margin-bottom: 16px; }}
    .star-btn {{
      font-size: 28px; cursor: pointer; background: none; border: none;
      color: #ddd; transition: color 0.15s;
    }}
    .star-btn.active {{ color: #f5a623; }}
    textarea {{
      width: 100%; height: 100px; border: 1px solid #ddd; border-radius: 8px;
      padding: 10px; font-size: 14px; resize: vertical; margin-bottom: 16px;
    }}
    .modal-btns {{ display: flex; gap: 8px; }}
    .btn-save {{
      flex: 2; background: #00b900; color: white;
      border: none; border-radius: 20px; padding: 10px; font-size: 14px; cursor: pointer;
    }}
    .btn-cancel {{
      flex: 1; background: #f0f0f0; color: #333;
      border: none; border-radius: 20px; padding: 10px; font-size: 14px; cursor: pointer;
    }}
    #toast {{
      display: none; position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
      background: #333; color: white; padding: 10px 20px; border-radius: 20px;
      font-size: 14px; z-index: 4000;
    }}
    #empty-msg {{
      position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%);
      text-align: center; color: #666; font-size: 15px; z-index: 1000;
      background: white; padding: 24px; border-radius: 16px;
      box-shadow: 0 4px 16px rgba(0,0,0,0.1);
    }}
  </style>
</head>
<body>
  <div id="header">
    <h1>🍽️ マイ飲食店マップ</h1>
    <span>{count} 件</span>
  </div>
  <div id="map"></div>

  {"" if markers_data else '<div id="empty-msg">📍 まだ確定したお店がありません<br><br>LINEで「ここに決定」ボタンを押すと<br>ここに追加されます！</div>'}

  <!-- レビュー編集モーダル -->
  <div id="modal-overlay">
    <div id="modal">
      <h2 id="modal-title">レビューを編集</h2>
      <div class="star-row" id="star-row">
        <button class="star-btn" data-v="1">★</button>
        <button class="star-btn" data-v="2">★</button>
        <button class="star-btn" data-v="3">★</button>
        <button class="star-btn" data-v="4">★</button>
        <button class="star-btn" data-v="5">★</button>
      </div>
      <textarea id="review-text" placeholder="感想を入力してください…"></textarea>
      <div class="modal-btns">
        <button class="btn-save" onclick="saveReview()">💾 保存</button>
        <button class="btn-cancel" onclick="closeModal()">キャンセル</button>
      </div>
    </div>
  </div>
  <div id="toast"></div>

  <script>
    var USER_ID = '{_escape_js(user_id)}';
    var REVIEW_API = '{_escape_js(review_api_url)}';
    var markers = {markers_json};
    var currentVisitId = null;
    var currentRating = 0;

    // マップ初期化
    var map = L.map('map').setView([{center_lat}, {center_lng}], {zoom});
    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
      attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
    }}).addTo(map);

    // マーカー追加
    markers.forEach(function(m) {{
      var imgsHtml = '';
      if (m.images && m.images.length > 0) {{
        imgsHtml = '<div class="popup-imgs">' +
          m.images.slice(0,4).map(function(u) {{
            return '<img src="' + u + '" alt="写真">';
          }}).join('') +
          '</div>';
      }}
      var popup =
        '<div class="popup-wrap">' +
        '<div class="popup-name">🍽️ ' + m.name + '</div>' +
        (m.genre ? '<div class="popup-genre">' + m.genre + '</div>' : '') +
        '<div class="popup-stars">' + m.stars + '</div>' +
        (m.review ? '<div class="popup-review">"' + m.review + '"</div>' : '') +
        (m.address ? '<div class="popup-address">📍 ' + m.address + '</div>' : '') +
        '<div class="popup-date">📅 ' + m.date + '</div>' +
        imgsHtml +
        (m.url ? '<a class="popup-link" href="' + m.url + '" target="_blank">🔗 Hotpepperで見る</a><br>' : '') +
        '<button class="btn-review" onclick="openModal(\\'' + m.visit_id + '\\', \\'' + m.name + '\\', ' + m.rating + ', \\'' + m.review + '\\')">✏️ レビューを編集</button>' +
        '</div>';

      L.marker([m.lat, m.lng])
        .addTo(map)
        .bindPopup(popup, {{ maxWidth: 300 }});
    }});

    // 複数マーカーがある場合は全体が見えるようにズーム
    if (markers.length > 1) {{
      var group = L.featureGroup(
        markers.map(function(m) {{ return L.marker([m.lat, m.lng]); }})
      );
      map.fitBounds(group.getBounds().pad(0.15));
    }}

    // ====== レビューモーダル ======
    function openModal(visitId, name, rating, review) {{
      currentVisitId = visitId;
      currentRating = rating || 0;
      document.getElementById('modal-title').textContent = '「' + name + '」のレビュー';
      document.getElementById('review-text').value = review || '';
      updateStars(currentRating);
      document.getElementById('modal-overlay').classList.add('show');
    }}

    function closeModal() {{
      document.getElementById('modal-overlay').classList.remove('show');
    }}

    function updateStars(val) {{
      document.querySelectorAll('.star-btn').forEach(function(btn) {{
        btn.classList.toggle('active', parseInt(btn.dataset.v) <= val);
      }});
    }}

    document.querySelectorAll('.star-btn').forEach(function(btn) {{
      btn.addEventListener('click', function() {{
        currentRating = parseInt(btn.dataset.v);
        updateStars(currentRating);
      }});
    }});

    function saveReview() {{
      var review = document.getElementById('review-text').value;
      fetch(REVIEW_API, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{
          user_id: USER_ID,
          visit_id: currentVisitId,
          rating: currentRating || null,
          review: review || null
        }})
      }})
      .then(function(r) {{ return r.json(); }})
      .then(function(d) {{
        if (d.ok) {{
          showToast('✅ レビューを保存しました！');
          closeModal();
          setTimeout(function() {{ location.reload(); }}, 1200);
        }} else {{
          showToast('❌ 保存に失敗しました');
        }}
      }})
      .catch(function() {{ showToast('❌ 保存に失敗しました'); }});
    }}

    function showToast(msg) {{
      var t = document.getElementById('toast');
      t.textContent = msg;
      t.style.display = 'block';
      setTimeout(function() {{ t.style.display = 'none'; }}, 2500);
    }}

    // モーダル外クリックで閉じる
    document.getElementById('modal-overlay').addEventListener('click', function(e) {{
      if (e.target === this) closeModal();
    }});
  </script>
</body>
</html>"""


# =========================================================
# Lambda ハンドラ
# =========================================================

def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:  # noqa: ARG001
    method = (event.get("httpMethod") or event.get("requestContext", {}).get("http", {}).get("method") or "GET").upper()
    path = event.get("path") or event.get("rawPath") or "/"
    map_base_url = os.environ.get("MAP_BASE_URL", "").strip()

    # ---- GET /map ---- マップHTMLを返す
    if method == "GET":
        params = event.get("queryStringParameters") or {}
        user_id = (params.get("user_id") or "").strip()
        if not user_id:
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "text/html; charset=utf-8"},
                "body": "<html><body><h1>user_idが必要です</h1></body></html>",
            }
        restaurants = get_user_restaurants(user_id)
        html = render_map_html(restaurants, user_id, map_base_url)
        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "text/html; charset=utf-8",
                "Cache-Control": "no-cache, no-store",
            },
            "body": html,
        }

    # ---- POST /map/review ---- レビュー更新API
    if method == "POST" and path.rstrip("/").endswith("/review"):
        try:
            body_str = event.get("body") or "{}"
            if event.get("isBase64Encoded"):
                import base64
                body_str = base64.b64decode(body_str).decode("utf-8")
            body = json.loads(body_str)
        except (json.JSONDecodeError, Exception) as e:
            logger.error(f"Failed to parse request body: {e}")
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"ok": False, "error": "invalid body"}),
            }

        user_id = str(body.get("user_id") or "").strip()
        visit_id = str(body.get("visit_id") or "").strip()
        rating_raw = body.get("rating")
        review_raw = body.get("review")

        if not user_id or not visit_id:
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"ok": False, "error": "user_id and visit_id required"}),
            }

        rating: Optional[int] = None
        if rating_raw is not None:
            try:
                r = int(rating_raw)
                rating = r if 1 <= r <= 5 else None
            except (ValueError, TypeError):
                pass

        review: Optional[str] = None
        if review_raw is not None and str(review_raw).strip():
            review = str(review_raw).strip()

        ok = update_review(user_id, visit_id, rating, review)
        status = 200 if ok else 500
        return {
            "statusCode": status,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*",
            },
            "body": json.dumps({"ok": ok}),
        }

    return {
        "statusCode": 404,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": "not found"}),
    }
