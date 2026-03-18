"""
setup_identity.py
=================
AgentCore Identity に Hotpepper と Tavily の認証情報を登録するセットアップスクリプト。

【実行前の準備】
  1. AWS 認証: `aws login` でサインイン
  2. 仮想環境を有効化: `source .venv/bin/activate`
  3. 環境変数を設定（または .env ファイルに記述）:
       HOTPEPPER_API_KEY=<Hotpepper APIキー>
       TAVILY_API_KEY=<Tavily APIキー>
       COGNITO_CLIENT_ID=<Cognito クライアントID>
       COGNITO_CLIENT_SECRET=<Cognito クライアントシークレット>
       COGNITO_DISCOVERY_URL=<Cognito OIDC Discovery URL>
         例: https://cognito-idp.us-west-2.amazonaws.com/<UserPoolId>/.well-known/openid-configuration

【Identity の仕組み】
  - 各APIの認証情報を Identity の OAuth2 プロバイダーとして登録する
  - Lambda Gateway ツールは @requires_access_token(auth_flow="M2M") で
    Identity からトークンを動的に取得し、APIを呼び出す
  - 認証情報はコード外で安全に管理されるため、ハードコードが不要になる

【Cognito セットアップ】
  AgentCore Identity は OAuth2 M2M フローを使うため、Cognito で以下が必要:
  1. User Pool を作成
  2. Resource Server を作成（スコープ: api:search）
  3. App Client（M2M用）を作成し、クライアントクレデンシャルを有効化

【実行方法】
  python setup/setup_identity.py
"""
import json
import os
import sys

import boto3
from botocore.exceptions import ClientError

# ========== 設定 ==========
REGION = os.environ.get("AGENTCORE_REGION", "us-west-2").strip()

# Identity に登録するプロバイダー名（agent.py / Lambda ツールと一致させること）
HOTPEPPER_PROVIDER_NAME = "HotpepperM2MProvider"
TAVILY_PROVIDER_NAME = "TavilyM2MProvider"
GATEWAY_AUTH_PROVIDER_NAME = "GatewayAuthProvider"

# 各APIの認証情報（環境変数から取得）
HOTPEPPER_API_KEY = os.environ.get("HOTPEPPER_API_KEY", "").strip()
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "").strip()

# Cognito の設定（M2M フロー用）
COGNITO_CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID", "").strip()
COGNITO_CLIENT_SECRET = os.environ.get("COGNITO_CLIENT_SECRET", "").strip()
COGNITO_DISCOVERY_URL = os.environ.get("COGNITO_DISCOVERY_URL", "").strip()


def _get_bedrock_agentcore_client():
    """bedrock-agentcore コントロールプレーンクライアントを取得"""
    return boto3.client("bedrock-agentcore-control", region_name=REGION)


def _check_provider_exists(client, provider_name: str) -> bool:
    """指定した名前のプロバイダーが既に存在するか確認"""
    try:
        providers = client.list_oauth2_credential_providers()
        for p in providers.get("credentialProviders", []):
            if p.get("name") == provider_name:
                return True
    except ClientError:
        pass
    return False


def register_hotpepper_provider(client) -> str:
    """
    Hotpepper M2M プロバイダーを Identity に登録する。

    Hotpepper は OAuth2 ではなく APIキー認証だが、
    Cognito を OAuth2 サーバーとして使い、クライアントクレデンシャルフローで
    発行したトークンに Hotpepper APIキーを含める構成にする。

    ※ 実際の運用では Cognito Lambda トリガー で token に api_key を埋め込む、
      または Secrets Manager から api_key を取得する形にする。
      このサンプルでは clientSecret に APIキーを設定する簡易構成を示す。
    """
    print(f"\n[1/3] Hotpepper M2M プロバイダーを登録: {HOTPEPPER_PROVIDER_NAME}")

    if _check_provider_exists(client, HOTPEPPER_PROVIDER_NAME):
        print(f"  ✓ 既に登録済みです: {HOTPEPPER_PROVIDER_NAME}")
        return HOTPEPPER_PROVIDER_NAME

    if not COGNITO_DISCOVERY_URL or not COGNITO_CLIENT_ID:
        print("  ⚠ COGNITO_DISCOVERY_URL / COGNITO_CLIENT_ID が未設定のためスキップします")
        print("    環境変数を設定して再実行してください")
        return ""

    response = client.create_oauth2_credential_provider(
        name=HOTPEPPER_PROVIDER_NAME,
        credentialProviderVendor="CustomOauth2",
        oauth2ProviderConfigInput={
            "customOauth2ProviderConfig": {
                "oauthDiscovery": {
                    "discoveryUrl": COGNITO_DISCOVERY_URL,
                },
                "clientId": COGNITO_CLIENT_ID,
                "clientSecret": COGNITO_CLIENT_SECRET or HOTPEPPER_API_KEY,
            }
        },
    )
    arn = response.get("credentialProviderArn", "")
    print(f"  ✓ 登録完了: ARN = {arn}")
    return arn


def register_tavily_provider(client) -> str:
    """
    Tavily M2M プロバイダーを Identity に登録する。
    Hotpepper と同様に Cognito を OAuth2 サーバーとして使用する。
    """
    print(f"\n[2/3] Tavily M2M プロバイダーを登録: {TAVILY_PROVIDER_NAME}")

    if _check_provider_exists(client, TAVILY_PROVIDER_NAME):
        print(f"  ✓ 既に登録済みです: {TAVILY_PROVIDER_NAME}")
        return TAVILY_PROVIDER_NAME

    if not COGNITO_DISCOVERY_URL or not COGNITO_CLIENT_ID:
        print("  ⚠ COGNITO_DISCOVERY_URL / COGNITO_CLIENT_ID が未設定のためスキップします")
        return ""

    response = client.create_oauth2_credential_provider(
        name=TAVILY_PROVIDER_NAME,
        credentialProviderVendor="CustomOauth2",
        oauth2ProviderConfigInput={
            "customOauth2ProviderConfig": {
                "oauthDiscovery": {
                    "discoveryUrl": COGNITO_DISCOVERY_URL,
                },
                "clientId": COGNITO_CLIENT_ID,
                "clientSecret": COGNITO_CLIENT_SECRET or TAVILY_API_KEY,
            }
        },
    )
    arn = response.get("credentialProviderArn", "")
    print(f"  ✓ 登録完了: ARN = {arn}")
    return arn


def register_gateway_auth_provider(client) -> str:
    """
    Gateway 認証用 プロバイダーを Identity に登録する。
    agent.py の @requires_access_token が Gateway 接続時に使うトークンを取得する。
    """
    print(f"\n[3/3] Gateway 認証プロバイダーを登録: {GATEWAY_AUTH_PROVIDER_NAME}")

    if _check_provider_exists(client, GATEWAY_AUTH_PROVIDER_NAME):
        print(f"  ✓ 既に登録済みです: {GATEWAY_AUTH_PROVIDER_NAME}")
        return GATEWAY_AUTH_PROVIDER_NAME

    if not COGNITO_DISCOVERY_URL or not COGNITO_CLIENT_ID:
        print("  ⚠ COGNITO_DISCOVERY_URL / COGNITO_CLIENT_ID が未設定のためスキップします")
        return ""

    response = client.create_oauth2_credential_provider(
        name=GATEWAY_AUTH_PROVIDER_NAME,
        credentialProviderVendor="CustomOauth2",
        oauth2ProviderConfigInput={
            "customOauth2ProviderConfig": {
                "oauthDiscovery": {
                    "discoveryUrl": COGNITO_DISCOVERY_URL,
                },
                "clientId": COGNITO_CLIENT_ID,
                "clientSecret": COGNITO_CLIENT_SECRET,
            }
        },
    )
    arn = response.get("credentialProviderArn", "")
    print(f"  ✓ 登録完了: ARN = {arn}")
    return arn


def save_config(hotpepper_arn: str, tavily_arn: str, gateway_arn: str) -> None:
    """登録結果を設定ファイルに保存"""
    config = {
        "region": REGION,
        "hotpepper_provider": {
            "name": HOTPEPPER_PROVIDER_NAME,
            "arn": hotpepper_arn,
        },
        "tavily_provider": {
            "name": TAVILY_PROVIDER_NAME,
            "arn": tavily_arn,
        },
        "gateway_auth_provider": {
            "name": GATEWAY_AUTH_PROVIDER_NAME,
            "arn": gateway_arn,
        },
    }
    config_path = "setup/identity_config.json"
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    print(f"\n✓ 設定を保存しました: {config_path}")


def main():
    print("=" * 60)
    print("AgentCore Identity セットアップ")
    print("=" * 60)
    print(f"リージョン: {REGION}")

    # 環境変数チェック
    missing = []
    if not HOTPEPPER_API_KEY:
        missing.append("HOTPEPPER_API_KEY")
    if not TAVILY_API_KEY:
        missing.append("TAVILY_API_KEY")
    if missing:
        print(f"\n⚠ 以下の環境変数が未設定です: {', '.join(missing)}")
        print("  Cognito 設定が揃っている場合は続行します（APIキーはスキップ）")

    client = _get_bedrock_agentcore_client()

    hotpepper_arn = register_hotpepper_provider(client)
    tavily_arn = register_tavily_provider(client)
    gateway_arn = register_gateway_auth_provider(client)

    save_config(hotpepper_arn, tavily_arn, gateway_arn)

    print("\n" + "=" * 60)
    print("Identity セットアップ完了！")
    print("次のステップ: python setup/setup_gateway.py を実行してください")
    print("=" * 60)


if __name__ == "__main__":
    main()
