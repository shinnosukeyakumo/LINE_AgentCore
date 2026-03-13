"""
AgentCore Runtime - マルチエージェント構成（Gateway統合版）

[アーキテクチャ]
  LINE → Lambda → AgentCore Runtime
                      ↓
               [監督エージェント]
                 ↓ ツールとして呼び出す
                 ├── [飲食店専門家エージェント]
                 │     → AgentCore Gateway (MCPClient)
                 │         → hotpepper_gateway_tool Lambda
                 │              → @requires_access_token で Hotpepper APIキー取得
                 └── [Web検索専門家エージェント]
                       → AgentCore Gateway (MCPClient)
                           → websearch_gateway_tool Lambda
                                → @requires_access_token で Tavily APIキー取得
"""
import json
import logging
import os
from typing import Any, Dict, Optional

from bedrock_agentcore import BedrockAgentCoreApp
from bedrock_agentcore.identity import requires_access_token
from strands import Agent, tool
from strands.models import BedrockModel

from http_utils import _norm_query, _yield_text_event
from session_state import (
    _add_preference,
    _clear_recommendations,
    _clear_shop_ready,
    _get_location,
    _get_preference_text,
    _get_recommendations,
    _parse_location_command,
    _pop_shop_ready,
    _session_id_from,
    _set_location,
    _set_recommendations,
    _set_shop_ready,
    _LOCATION_CMD_PREFIX,
)

logger = logging.getLogger(__name__)

# ========== ENV ==========
MODEL_ID = (os.environ.get("MODEL_ID") or "us.anthropic.claude-haiku-4-5-20251001-v1:0").strip()
AGENTCORE_MEMORY_ID = (
    os.environ.get("AGENTCORE_MEMORY_ID")
    or os.environ.get("BEDROCK_AGENTCORE_MEMORY_ID")
    or "line_agentcore_mem-9qQz574MzM"
).strip()
AGENTCORE_REGION = os.environ.get("AGENTCORE_REGION", "us-west-2").strip()
_CONFIRM_CMD_PREFIX = "__confirm__"

# ========== Gateway 設定 ==========
# setup/setup_gateway.py 実行後に出力される URL と プロバイダー名を設定
GATEWAY_URL = os.environ.get("GATEWAY_URL", "").strip()          # 例: https://xxxx.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp
GATEWAY_PROVIDER_NAME = os.environ.get("GATEWAY_PROVIDER_NAME", "GatewayAuthProvider").strip()
GATEWAY_SCOPES = ["LINE-AgentCore-SearchGateway/invoke"]

# フォールバック用APIキー（Gateway が使えない場合のみ利用）
HOTPEPPER_API_KEY = os.environ.get("HOTPEPPER_API_KEY", "").strip()
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "").strip()

# ========== AgentCoreMemorySessionManager（オプション） ==========
_MEMORY_SESSION_AVAILABLE = False
try:
    from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
    from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
    _MEMORY_SESSION_AVAILABLE = True
except ImportError:
    logger.info("AgentCoreMemorySessionManager not available (import failed)")

# ========== エージェントキャッシュ ==========
_SESSION_SUPERVISORS: Dict[str, Agent] = {}
_SESSION_MEMORY_MANAGERS: Dict[str, Any] = {}
_SESSION_RESTAURANT_AGENTS: Dict[str, Agent] = {}
_WEBSEARCH_AGENT: Optional[Agent] = None

app = BedrockAgentCoreApp()


# ==============================================================
# Gateway MCPClient ヘルパー
# MCPClient のコンテキストを開いたまま agent を実行することが必須
# ==============================================================

@requires_access_token(
    provider_name=GATEWAY_PROVIDER_NAME,
    scopes=GATEWAY_SCOPES,
    auth_flow="M2M",
    into="gateway_token",
)
def _run_restaurant_via_gateway(query: str, session_id: str, *, gateway_token: str = "") -> str:
    """
    MCPClient コンテキストを開いたまま飲食店エージェントを実行する。
    confirm_shop ツールで Gateway Lambda が返す shop_json を捕捉し、
    _set_recommendations() に保存することで __SHOP_CONFIRM__ マーカーを有効にする。
    """
    import httpx
    from mcp.client.streamable_http import streamable_http_client
    from strands.tools.mcp import MCPClient

    token = gateway_token
    mcp_client = MCPClient(
        lambda: streamable_http_client(
            GATEWAY_URL,
            http_client=httpx.AsyncClient(
                headers={"Authorization": f"Bearer {token}"},
            ),
        )
    )
    with mcp_client:
        tools = mcp_client.list_tools_sync()
        tool_names = [getattr(t, "name", str(t)) for t in tools]
        logger.info("Gateway ツール一覧: %s", tool_names)
        hotpepper_tools = [
            t for t in tools
            if "hotpepper" in (getattr(t, "tool_name", getattr(t, "name", "")) or "").lower()
        ]
        if not hotpepper_tools:
            logger.warning("Gateway に hotpepper ツールが見つかりません（ツール数: %d, 名前: %s）", len(tools), tool_names)
            return ""

        @tool
        def confirm_shop(shop_json: str) -> str:
            """
            hotpepper_search が成功して shop_json が返ってきた場合に必ず呼び出すツール。
            shop_json: hotpepper_search レスポンスの shop_json フィールドの値（JSON文字列）
            """
            try:
                shop = json.loads(shop_json)
                _set_recommendations(session_id, [shop], limit=1)
                logger.info("Gateway: shop saved for confirmation: %s", shop.get("name", "?"))
            except Exception as exc:
                logger.warning("confirm_shop failed: %s", exc)
            return "保存完了"

        loc = _get_location(session_id)
        loc_text = (
            f"保存済み位置情報: lat={loc.get('lat')}, lng={loc.get('lng')}, "
            f"title={loc.get('title') or '-'}, address={loc.get('address') or '-'}"
            if loc else "保存済み位置情報: 未設定"
        )
        pref_text = _get_preference_text(session_id)
        memory_section = f"\n{pref_text}" if pref_text else ""

        system_prompt = f"""あなたは飲食店検索の専門家エージェントです。
使えるツール:
- hotpepper_search: 位置情報とキーワードでHotpepperを検索する
- confirm_shop: 検索で見つかった店舗データを保存する（必須）

手順:
1. hotpepper_search(lat=<緯度>, lng=<経度>, keyword=<ジャンル>)を呼び出す
2. shop_jsonが含まれていたら、必ずconfirm_shop(shop_json=<shop_jsonの値>)を呼ぶ
3. 店名・ジャンル・予算・営業時間を提示する（住所・座標・地図リンクは出さない）

ルール:
・検索結果は必ず1件のみ提示。
・shop_jsonがある場合はconfirm_shopを必ず呼ぶこと。
・出力はLINE向けに簡潔な日本語。Markdownは使わない。

{loc_text}{memory_section}"""

        logger.info("Gateway MCP 飲食店エージェントを実行: ツール=%s", [getattr(t, "tool_name", getattr(t, "name", "")) for t in hotpepper_tools])
        agent = Agent(
            model=BedrockModel(model_id=MODEL_ID),
            system_prompt=system_prompt,
            tools=[*hotpepper_tools, confirm_shop],
        )
        return str(agent(query))


@requires_access_token(
    provider_name=GATEWAY_PROVIDER_NAME,
    scopes=GATEWAY_SCOPES,
    auth_flow="M2M",
    into="gateway_token",
)
def _run_websearch_via_gateway(query: str, *, gateway_token: str = "") -> str:
    """
    MCPClient コンテキストを開いたまま Web 検索エージェントを実行する。
    """
    import httpx
    from mcp.client.streamable_http import streamable_http_client
    from strands.tools.mcp import MCPClient

    token = gateway_token
    mcp_client = MCPClient(
        lambda: streamable_http_client(
            GATEWAY_URL,
            http_client=httpx.AsyncClient(
                headers={"Authorization": f"Bearer {token}"},
            ),
        )
    )
    with mcp_client:
        tools = mcp_client.list_tools_sync()
        search_tools = [
            t for t in tools
            if "web_search" in (getattr(t, "tool_name", getattr(t, "name", "")) or "").lower()
        ]
        if not search_tools:
            logger.warning("Gateway に web_search ツールが見つかりません（ツール数: %d）", len(tools))
            return ""
        logger.info("Gateway MCP Web 検索エージェントを実行: ツール=%s", [getattr(t, "tool_name", getattr(t, "name", "")) for t in search_tools])
        agent = Agent(
            model=BedrockModel(model_id=MODEL_ID),
            system_prompt="""あなたはWeb検索の専門家エージェントです。
web_searchツールを使って質問に回答してください。
・出力はLINE向けに簡潔な日本語。Markdownは使わない。
・要点のみに絞り300字以内でまとめる。
""",
            tools=search_tools,
        )
        return str(agent(query))


# ==============================================================
# 専門家エージェント 1: 飲食店検索
# ==============================================================

def _restaurant_system_prompt(session_id: str) -> str:
    loc = _get_location(session_id)
    loc_text = (
        f"保存済み位置情報: lat={loc.get('lat')}, lng={loc.get('lng')}, "
        f"title={loc.get('title') or '-'}, address={loc.get('address') or '-'}"
        if loc else "保存済み位置情報: 未設定"
    )
    pref_text = _get_preference_text(session_id)
    memory_section = f"\n{pref_text}" if pref_text else ""

    return f"""あなたは飲食店検索の専門家エージェントです。
使えるツール:
- restaurant_search: キーワードでHotpepperを検索し近くの店舗を提案する（必ず使う）
- restaurant_location: ユーザーが選んだ店舗の位置情報（住所・地図）を返す

ルール:
・ユーザーに飲食店を提案する際は、必ずrestaurant_searchを呼び出すこと。知識で返答しないこと。
・検索結果は最大3件を番号付きで提示。住所・座標・地図リンクはこの時点では出さない。
・「ここに決定」ボタン押下後のみ restaurant_location で位置情報を返す。
・ユーザー嗜好が提供されている場合は最優先で反映する。
・過去の訪問済みお店は避けて新しい発見を提案する。
・出力はLINE向けに簡潔な日本語。Markdownは使わない。

{loc_text}{memory_section}"""


def _build_restaurant_specialist(session_id: str) -> Agent:
    """
    飲食店専門エージェント（フォールバック用）。
    HOTPEPPER_API_KEY 環境変数を直接使用する。
    Gateway が使える場合は _run_restaurant_via_gateway() を優先利用する。
    """
    from hotpepper import (
        _format_candidate_names,
        _format_selected_shop_location,
        _search_hotpepper_with_expansion,
        _select_shop_from_recommendations,
    )

    @tool
    def restaurant_search(keyword: str, range_level: int = 2, count: int = 20) -> str:
        """
        保存済み位置情報を基準に、Hotpepperで飲食店を検索します。
        range_level: 1=300m,2=500m,3=1000m,4=2000m,5=3000m
        """
        print(f"[DEBUG restaurant_search] CALLED keyword={keyword!r} range_level={range_level}", flush=True)
        loc = _get_location(session_id)
        if not loc:
            print(f"[DEBUG restaurant_search] NO LOCATION session_id={session_id}", flush=True)
            return "位置情報が未設定です。LINEの＋ボタンから位置情報を送ってください。"
        if not HOTPEPPER_API_KEY:
            print("[DEBUG restaurant_search] NO API KEY", flush=True)
            return "Hotpepper APIキーが設定されていません。"
        print(f"[DEBUG restaurant_search] lat={loc['lat']} lng={loc['lng']} session_id={session_id}", flush=True)
        result = _search_hotpepper_with_expansion(
            session_id=session_id,
            lat=float(loc["lat"]),
            lng=float(loc["lng"]),
            keyword=keyword,
            start_range=range_level,
            count=count,
            api_key=HOTPEPPER_API_KEY,
        )
        print(f"[DEBUG restaurant_search] RESULT_PREVIEW={result[:80]!r}", flush=True)
        return result

    @tool
    def restaurant_location(choice: str) -> str:
        """
        直近で提示した候補からユーザーが選んだ店舗の位置情報を返します。
        choice: 例「1件目」「店名」「これがいい」
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

    return Agent(
        model=BedrockModel(model_id=MODEL_ID),
        system_prompt=_restaurant_system_prompt(session_id),
        tools=[restaurant_search, restaurant_location],
    )


def _get_or_create_restaurant_agent(session_id: str) -> Agent:
    if session_id not in _SESSION_RESTAURANT_AGENTS:
        _SESSION_RESTAURANT_AGENTS[session_id] = _build_restaurant_specialist(session_id)
    else:
        _SESSION_RESTAURANT_AGENTS[session_id].system_prompt = _restaurant_system_prompt(session_id)
    return _SESSION_RESTAURANT_AGENTS[session_id]


# ==============================================================
# 専門家エージェント 2: Web検索
# ==============================================================

def _build_websearch_specialist() -> Agent:
    """
    Web 検索専門エージェント（フォールバック用）。
    TAVILY_API_KEY 環境変数を直接使用する。
    Gateway が使える場合は _run_websearch_via_gateway() を優先利用する。
    """
    from web_search import _web_search

    @tool
    def websearch(query: str, max_results: int = 5) -> str:
        """Tavilyを使ってWeb検索し、要点をまとめます。"""
        if not TAVILY_API_KEY:
            return "Tavily APIキーが設定されていません。"
        return _web_search(query=query, max_results=max_results, api_key=TAVILY_API_KEY)

    return Agent(
        model=BedrockModel(model_id=MODEL_ID),
        system_prompt="""あなたはWeb検索の専門家エージェントです。
websearchツールを使って質問に回答してください。
・出力はLINE向けに簡潔な日本語。Markdownは使わない。
・要点のみに絞り300字以内でまとめる。
""",
        tools=[websearch],
    )


def _get_or_create_websearch_agent() -> Agent:
    global _WEBSEARCH_AGENT
    if _WEBSEARCH_AGENT is None:
        _WEBSEARCH_AGENT = _build_websearch_specialist()
    return _WEBSEARCH_AGENT


# ==============================================================
# 監督エージェント
# ==============================================================

def _supervisor_system_prompt(session_id: str) -> str:
    loc = _get_location(session_id)
    loc_text = (
        f"現在地: {loc.get('title') or ''} / {loc.get('address') or ''}"
        if loc else "現在地: 未設定（飲食店検索が必要な場合はLINEの＋ボタンから位置情報共有を依頼してください）"
    )
    return f"""あなたはLINEで動く飲食店サポートAIの監督エージェントです。
ユーザーのメッセージを解析し、適切な専門エージェントに委譲してください。

委譲の判断基準:
・飲食店の検索・おすすめ・場所案内・店選び → delegate_to_restaurant
・「2件目にする」「3件目がいい」「1件目で」「これにする」「その店」など候補選択・確認 → delegate_to_restaurant
・「もう一度」「再検索」「別の店」「他の候補」など再提案依頼 → delegate_to_restaurant
・天気・ニュース・一般知識など飲食店以外の質問 → delegate_to_websearch
・挨拶・雑談 → ツール不要、直接返答してください

厳守ルール:
・「delegate_to_restaurant」「delegate_to_websearch」などのツール名を出力に絶対に含めないこと。
・「〇〇に委譲します」「専門エージェントに依頼します」などの処理説明もユーザーに見せないこと。
・専門エージェントの返答をそのままユーザーに届けること。自分で要約・改変・追記しないこと。
・マークダウン記法（**太字**、*斜体*、# 見出し、- 箇条書き）を一切使わないこと。プレーンテキストのみ。
・「2件目にする」「これにする」のような曖昧なメッセージでも必ず delegate_to_restaurant に委譲すること。自分で判断して直接返答しないこと。
・出力はLINE向けに簡潔な日本語。

{loc_text}"""


def _build_supervisor(session_id: str, session_manager: Any = None) -> Agent:
    """監督エージェント: ユーザーの意図を解析し専門家エージェントに委譲する"""

    @tool
    def delegate_to_restaurant(query: str) -> str:
        """
        飲食店の検索・おすすめ・位置案内に関するリクエストを
        飲食店専門エージェントに委譲します。
        """
        # 直接 API 優先: 毎回新規エージェントを作成し必ず restaurant_search を呼ばせる
        # （会話履歴キャッシュを使うと _set_recommendations が呼ばれず Flex ボタンが出ない）
        if HOTPEPPER_API_KEY:
            print(f"[DEBUG delegate_to_restaurant] START session_id={session_id} query={query[:80]!r}", flush=True)
            from session_state import _SESSION_SHOP_READY
            before_keys = list(_SESSION_SHOP_READY.keys())
            print(f"[DEBUG delegate_to_restaurant] SHOP_READY_BEFORE={before_keys}", flush=True)
            agent = _build_restaurant_specialist(session_id)
            result = str(agent(query))
            after_keys = list(_SESSION_SHOP_READY.keys())
            print(f"[DEBUG delegate_to_restaurant] SHOP_READY_AFTER={after_keys}", flush=True)
            print(f"[DEBUG delegate_to_restaurant] DONE result_len={len(result)} result_preview={result[:100]!r}", flush=True)
            return result
        # フォールバック: Gateway 経由（HOTPEPPER_API_KEY がない場合のみ）
        if GATEWAY_URL:
            try:
                result = _run_restaurant_via_gateway(query=query, session_id=session_id)
                if result:
                    return result
                logger.warning("Gateway 飲食店検索が空を返しました。")
            except Exception as exc:
                logger.error("Gateway 飲食店検索エラー: %s", exc)
        return "申し訳ありません。検索できませんでした。"

    @tool
    def delegate_to_websearch(query: str) -> str:
        """
        飲食店以外の一般情報やWeb検索リクエストを
        Web検索専門エージェントに委譲します。
        """
        if GATEWAY_URL:
            try:
                result = _run_websearch_via_gateway(query=query)
                if result:
                    return result
                logger.warning("Gateway Web 検索が空を返しました。フォールバックへ切り替え")
            except Exception as exc:
                logger.error("Gateway Web 検索エラー: %s", exc)
        # フォールバック: 直接 API 呼び出し（TAVILY_API_KEY env var が必要）
        agent = _get_or_create_websearch_agent()
        return str(agent(query))

    agent_kwargs: Dict[str, Any] = {
        "model": BedrockModel(model_id=MODEL_ID),
        "system_prompt": _supervisor_system_prompt(session_id),
        "tools": [delegate_to_restaurant, delegate_to_websearch],
    }
    if session_manager is not None:
        agent_kwargs["session_manager"] = session_manager
    return Agent(**agent_kwargs)


def _get_or_create_supervisor(session_id: str) -> Agent:
    # 毎回新規作成: AgentCoreMemorySessionManager が古い会話履歴を復元して
    # delegate_to_restaurant を呼ばなくなる問題を防ぐ。
    # 嗜好情報は _supervisor_system_prompt 内の _get_preference_text で渡す。
    return _build_supervisor(session_id, session_manager=None)


# ==============================================================
# 決定論ハンドリング（LLM不要な特殊コマンドのみ）
# ==============================================================

def _handle_special_command(prompt: str, session_id: str) -> Optional[str]:
    """__confirm__ / __set_location__ / 候補番号選択 を決定論で処理する"""
    import re as _re
    q = _norm_query(prompt)

    if q.startswith(_CONFIRM_CMD_PREFIX):
        body = q[len(_CONFIRM_CMD_PREFIX):].strip()
        try:
            shop = json.loads(body)
            name = (shop.get("name") or "").strip()
            genre = ((shop.get("genre") or {}).get("name") or "").strip()
            _add_preference(session_id, genre, name)
            genre_str = f"（{genre}）" if genre else ""
            return f"「{name}」{genre_str}への訪問を記憶しました！次のおすすめに活かします。"
        except (json.JSONDecodeError, Exception):
            return "訪問を記憶しました。"

    if q.startswith(_LOCATION_CMD_PREFIX):
        parsed = _parse_location_command(q)
        if parsed is None:
            return "位置情報の形式が不正でした。もう一度、LINEの＋ボタンから位置情報を送ってください。"
        lat, lng, title, address = parsed
        _set_location(session_id, lat, lng, title, address)
        _clear_recommendations(session_id)
        loc = _get_location(session_id) or {}
        msg = ["位置情報を保存しました。", f"座標: {loc.get('lat')}, {loc.get('lng')}"]
        if loc.get("address"):
            msg.append(f"住所: {loc.get('address')}")
        msg.append("例: 近くのラーメン屋 / この辺の居酒屋")
        return "\n".join(msg)

    # 候補番号選択: 「2件目にする」「3番がいい」など → LLM 不要で決定論的に処理
    # 数字 + 件目/番/つ目/番目 のパターンのみを対象とし誤検知を防ぐ
    m = _re.search(r"([1-9][0-9]?)\s*(件目|番目|番|つ目)", q)
    if m:
        shops = _get_recommendations(session_id)
        if shops:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(shops):
                from hotpepper import _format_selected_shop_location
                selected = shops[idx]
                _set_shop_ready(session_id, selected)
                print(f"[DEBUG _handle_special_command] candidate_select idx={idx} shop={selected.get('name')}", flush=True)
                return _format_selected_shop_location(selected)

    return None


# ==============================================================
# AgentCore entrypoint
# ==============================================================

@app.entrypoint
async def invoke_agent(payload: Dict[str, Any], context: Any):  # noqa: ARG001
    prompt = (payload.get("prompt") or "").strip()
    session_id = _session_id_from(payload, context)

    if not MODEL_ID:
        for ev in _yield_text_event("MODEL_ID が未設定です。AgentCore Runtime の環境変数 MODEL_ID を設定してください。"):
            yield ev
        return

    _clear_shop_ready(session_id)

    result = _handle_special_command(prompt, session_id)
    if result is not None:
        for ev in _yield_text_event(result):
            yield ev
        # 候補番号選択（「2件目にする」等）の場合、_set_shop_ready が呼ばれているので
        # Flex ボタンマーカーも送出する（__confirm__ / __set_location__ では None が返る）
        shop = _pop_shop_ready(session_id)
        print(f"[DEBUG invoke_agent] SPECIAL_SHOP_CONFIRM_CHECK: shop={shop.get('name') if shop else None}", flush=True)
        if shop:
            all_shops = _get_recommendations(session_id)
            marker_data = {"current": shop, "all": all_shops}
            marker = f"__SHOP_CONFIRM__:{json.dumps(marker_data, ensure_ascii=False)}"
            for ev in _yield_text_event(marker):
                yield ev
        return

    supervisor = _get_or_create_supervisor(session_id=session_id)
    try:
        async for event in supervisor.stream_async(prompt):
            yield event
    except Exception:
        for ev in _yield_text_event("Runtime内部でエラーが発生しました。もう一度お試しください。"):
            yield ev
        return

    shop = _pop_shop_ready(session_id)
    print(f"[DEBUG invoke_agent] SHOP_CONFIRM_CHECK: session_id={session_id} shop={shop.get('name') if shop else None}", flush=True)
    if shop:
        all_shops = _get_recommendations(session_id)
        marker_data = {"current": shop, "all": all_shops}
        marker = f"__SHOP_CONFIRM__:{json.dumps(marker_data, ensure_ascii=False)}"
        print(f"[DEBUG invoke_agent] SHOP_CONFIRM_SENDING: shop={shop.get('name')} all_count={len(all_shops)}", flush=True)
        for ev in _yield_text_event(marker):
            yield ev


if __name__ == "__main__":
    app.run()
