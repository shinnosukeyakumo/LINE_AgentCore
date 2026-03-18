"""
setup_gateway.py
================
AgentCore Gateway を作成し、2つの Lambda ツールを登録するセットアップスクリプト。

【実行前の準備】
  1. setup/setup_identity.py を先に実行して Identity を設定
  2. 2つの Lambda 関数をデプロイ済みであること:
       - hotpepper_gateway_tool (lambda/hotpepper_gateway_tool.py)
       - websearch_gateway_tool  (lambda/websearch_gateway_tool.py)
  3. 環境変数を設定:
       HOTPEPPER_LAMBDA_ARN=<Lambda の ARN>
       WEBSEARCH_LAMBDA_ARN=<Lambda の ARN>
       GATEWAY_ROLE_ARN=<Gateway 用 IAM ロールの ARN>（省略時は自動作成）

【Gateway の仕組み】
  Gateway は MCP (Model Context Protocol) サーバーとして機能する。
  エージェントは MCPClient で Gateway に接続し、登録されたツールを呼び出す。
  Gateway が Lambda を呼び出す際に workload access token をヘッダーに付与するので、
  Lambda 内で @requires_access_token が Identity からトークンを取得できる。

【構成】
  agent.py
    ↓ @requires_access_token (GatewayAuthProvider) でGateway認証トークン取得
    ↓ MCPClient で Gateway MCP エンドポイントに接続
  AgentCore Gateway (MCP サーバー)
    ↓ hotpepper_search ツール呼び出し → hotpepper_gateway_tool Lambda
    ↓ web_search ツール呼び出し      → websearch_gateway_tool Lambda
  各 Lambda
    ↓ @requires_access_token (M2M) で各 APIキーを Identity から取得
    → Hotpepper / Tavily API を呼び出す

【実行方法】
  python setup/setup_gateway.py
"""
import json
import os
import time

import boto3
from botocore.exceptions import ClientError

# ========== 設定 ==========
REGION = os.environ.get("AGENTCORE_REGION", "us-west-2").strip()
GATEWAY_NAME = "LINE-AgentCore-SearchGateway"

HOTPEPPER_LAMBDA_ARN = os.environ.get("HOTPEPPER_LAMBDA_ARN", "").strip()
WEBSEARCH_LAMBDA_ARN = os.environ.get("WEBSEARCH_LAMBDA_ARN", "").strip()
GATEWAY_ROLE_ARN = os.environ.get("GATEWAY_ROLE_ARN", "").strip()

# Identity で登録したプロバイダー名（setup_identity.py と一致）
GATEWAY_AUTH_PROVIDER_NAME = "GatewayAuthProvider"


def _get_control_client():
    return boto3.client("bedrock-agentcore-control", region_name=REGION)


def _load_identity_config() -> dict:
    """setup_identity.py が生成した設定ファイルを読み込む"""
    config_path = "setup/identity_config.json"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"⚠ {config_path} が見つかりません。setup_identity.py を先に実行してください。")
        return {}


def create_gateway(client) -> dict:
    """
    AgentCore Gateway を作成する。
    Gateway は自動的に Cognito を使った OAuth2 認証エンドポイントを用意する。
    """
    print(f"\n[1/3] Gateway を作成: {GATEWAY_NAME}")

    # 既存の Gateway を確認
    try:
        gateways = client.list_gateways()
        for gw in gateways.get("items", []):
            if gw.get("name") == GATEWAY_NAME:
                print(f"  ✓ 既に存在します: {gw.get('gatewayId')}")
                return gw
    except ClientError as e:
        print(f"  ⚠ Gateway 一覧取得エラー: {e}")

    kwargs = {
        "name": GATEWAY_NAME,
        "description": "LINE AgentCore: Hotpepper + Tavily 検索 Gateway",
        "protocolType": "MCP",
    }
    if GATEWAY_ROLE_ARN:
        kwargs["roleArn"] = GATEWAY_ROLE_ARN

    response = client.create_gateway(**kwargs)
    gateway = response.get("gateway") or response
    gateway_id = gateway.get("gatewayId") or gateway.get("id", "")
    print(f"  ✓ Gateway 作成完了: ID = {gateway_id}")

    # Gateway が ACTIVE になるまで待機
    print("  Gateway が ACTIVE になるまで待機中...", end="", flush=True)
    for _ in range(30):
        time.sleep(5)
        print(".", end="", flush=True)
        try:
            status_resp = client.get_gateway(gatewayId=gateway_id)
            status = (status_resp.get("gateway") or status_resp).get("status", "")
            if status == "ACTIVE":
                print(" ACTIVE ✓")
                break
        except ClientError:
            pass
    else:
        print(" タイムアウト（手動で確認してください）")

    return gateway


def _build_hotpepper_tool_schema() -> str:
    """Hotpepper 検索ツールの OpenAPI スキーマを生成する"""
    schema = {
        "openapi": "3.0.0",
        "info": {
            "title": "Hotpepper 飲食店検索",
            "version": "1.0.0",
            "description": "指定した位置情報とキーワードで Hotpepper API を使って飲食店を検索します",
        },
        "paths": {
            "/search": {
                "post": {
                    "operationId": "hotpepper_search",
                    "summary": "位置情報周辺の飲食店を検索して1件提案する",
                    "description": (
                        "保存済み位置情報を基準に Hotpepper で飲食店を検索します。"
                        "range_level: 1=300m, 2=500m, 3=1km, 4=2km, 5=3km"
                    ),
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["lat", "lng", "keyword"],
                                    "properties": {
                                        "lat": {"type": "number", "description": "緯度"},
                                        "lng": {"type": "number", "description": "経度"},
                                        "keyword": {"type": "string", "description": "検索キーワード（例: ラーメン、居酒屋）"},
                                        "range_level": {"type": "integer", "default": 2, "minimum": 1, "maximum": 5},
                                        "count": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "検索結果",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "result": {"type": "string"},
                                            "shop_count": {"type": "integer"},
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
    }
    return json.dumps(schema, ensure_ascii=False)


def _build_websearch_tool_schema() -> str:
    """Web 検索ツールの OpenAPI スキーマを生成する"""
    schema = {
        "openapi": "3.0.0",
        "info": {
            "title": "Tavily Web 検索",
            "version": "1.0.0",
            "description": "Tavily API を使って Web 検索し、要点をまとめます",
        },
        "paths": {
            "/search": {
                "post": {
                    "operationId": "web_search",
                    "summary": "Web 検索して結果の要点をまとめる",
                    "description": "Tavily を使って Web 検索し、要点を300字以内でまとめます",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["query"],
                                    "properties": {
                                        "query": {"type": "string", "description": "検索クエリ"},
                                        "max_results": {"type": "integer", "default": 5, "minimum": 1, "maximum": 10},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "検索結果",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "result": {"type": "string"},
                                            "result_count": {"type": "integer"},
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
    }
    return json.dumps(schema, ensure_ascii=False)


def register_hotpepper_tool(client, gateway_id: str) -> str:
    """
    Hotpepper 検索 Lambda を Gateway ツールとして登録する。
    スキーマは OpenAPI 形式で記述し、Gateway が MCP ツールに変換する。
    """
    print(f"\n[2/3] Hotpepper 検索ツールを Gateway に登録")

    if not HOTPEPPER_LAMBDA_ARN:
        print("  ⚠ HOTPEPPER_LAMBDA_ARN が未設定のためスキップします")
        print("    Lambda をデプロイして ARN を設定してください")
        return ""

    response = client.create_gateway_target(
        gatewayId=gateway_id,
        name="hotpepper-search",
        description="Hotpepper APIを使った飲食店検索ツール（@requires_access_token でAPIキーを動的取得）",
        targetConfiguration={
            "mcp": {
                "lambda": {
                    "lambdaArn": HOTPEPPER_LAMBDA_ARN,
                    "toolSchema": {
                        "inlinePayload": [
                            {
                                "name": "hotpepper_search",
                                "description": "位置情報周辺の飲食店を Hotpepper で検索して1件提案する",
                                "inputSchema": {
                                    "type": "object",
                                    "required": ["lat", "lng", "keyword"],
                                    "properties": {
                                        "lat": {"type": "number", "description": "緯度"},
                                        "lng": {"type": "number", "description": "経度"},
                                        "keyword": {"type": "string", "description": "検索キーワード（例: ラーメン、居酒屋）"},
                                        "range_level": {"type": "integer", "description": "検索範囲 1=300m〜5=3km（デフォルト: 2）"},
                                        "count": {"type": "integer", "description": "取得件数（デフォルト: 5）"},
                                    },
                                },
                            }
                        ]
                    },
                }
            }
        },
    )
    target_id = (response.get("gatewayTarget") or response).get("targetId", "")
    print(f"  ✓ Hotpepper ツール登録完了: targetId = {target_id}")
    return target_id


def register_websearch_tool(client, gateway_id: str) -> str:
    """
    Web 検索 Lambda を Gateway ツールとして登録する。
    """
    print(f"\n[3/3] Web 検索ツールを Gateway に登録")

    if not WEBSEARCH_LAMBDA_ARN:
        print("  ⚠ WEBSEARCH_LAMBDA_ARN が未設定のためスキップします")
        print("    Lambda をデプロイして ARN を設定してください")
        return ""

    response = client.create_gateway_target(
        gatewayId=gateway_id,
        name="web-search",
        description="Tavily APIを使ったWeb検索ツール（@requires_access_token でAPIキーを動的取得）",
        targetConfiguration={
            "mcp": {
                "lambda": {
                    "lambdaArn": WEBSEARCH_LAMBDA_ARN,
                    "toolSchema": {
                        "inlinePayload": [
                            {
                                "name": "web_search",
                                "description": "Tavily を使って Web 検索し、要点を300字以内でまとめる",
                                "inputSchema": {
                                    "type": "object",
                                    "required": ["query"],
                                    "properties": {
                                        "query": {"type": "string", "description": "検索クエリ"},
                                        "max_results": {"type": "integer", "description": "取得件数（デフォルト: 5）"},
                                    },
                                },
                            }
                        ]
                    },
                }
            }
        },
    )
    target_id = (response.get("gatewayTarget") or response).get("targetId", "")
    print(f"  ✓ Web 検索ツール登録完了: targetId = {target_id}")
    return target_id


def save_gateway_config(gateway: dict, hotpepper_target_id: str, websearch_target_id: str) -> None:
    """Gateway 情報を設定ファイルに保存し、必要な環境変数を表示する"""
    gateway_id = gateway.get("gatewayId") or gateway.get("id", "")
    gateway_url = gateway.get("gatewayUrl") or gateway.get("endpoint", "")

    config = {
        "gateway_id": gateway_id,
        "gateway_url": gateway_url,
        "region": REGION,
        "targets": {
            "hotpepper_search": hotpepper_target_id,
            "web_search": websearch_target_id,
        },
    }

    config_path = "setup/gateway_config.json"
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    print(f"\n✓ 設定を保存しました: {config_path}")
    print("\n" + "=" * 60)
    print("【Runtime に設定する環境変数】")
    print("=" * 60)
    if gateway_url:
        print(f"GATEWAY_URL={gateway_url}/mcp")
    else:
        print(f"GATEWAY_URL=https://<gateway-id>.gateway.bedrock-agentcore.{REGION}.amazonaws.com/mcp")
    print(f"GATEWAY_PROVIDER_NAME={GATEWAY_AUTH_PROVIDER_NAME}")
    print(f"AGENTCORE_REGION={REGION}")
    print("=" * 60)
    print("\nこれらを AgentCore Runtime の環境変数に設定してください。")
    print("（src/.bedrock_agentcore/line_agentcore/config.yaml または AWS コンソール）")


def main():
    print("=" * 60)
    print("AgentCore Gateway セットアップ")
    print("=" * 60)
    print(f"リージョン: {REGION}")
    print(f"Gateway 名: {GATEWAY_NAME}")

    identity_config = _load_identity_config()
    if not identity_config:
        print("\n先に setup/setup_identity.py を実行してください。")
        return

    client = _get_control_client()

    # Gateway を作成
    gateway = create_gateway(client)
    gateway_id = gateway.get("gatewayId") or gateway.get("id", "")
    if not gateway_id:
        print("❌ Gateway の作成に失敗しました。")
        return

    # ツールを登録
    hotpepper_target_id = register_hotpepper_tool(client, gateway_id)
    websearch_target_id = register_websearch_tool(client, gateway_id)

    # 設定を保存・表示
    save_gateway_config(gateway, hotpepper_target_id, websearch_target_id)

    print("\n" + "=" * 60)
    print("Gateway セットアップ完了！🎉")
    print("=" * 60)
    print("\n【次のステップ】")
    print("1. 上記の環境変数を AgentCore Runtime に設定する")
    print("2. Runtime をデプロイする: agentcore deploy")
    print("3. LINE でメッセージを送って動作確認する")


if __name__ == "__main__":
    main()
