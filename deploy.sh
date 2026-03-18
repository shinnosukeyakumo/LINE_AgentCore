#!/bin/bash
# =============================================================
# LINE AgentCore - デプロイスクリプト
# 使い方: ./deploy.sh
# =============================================================
set -e

# スクリプトの場所を絶対パスで固定（どこから実行しても動く）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

AGENT_NAME="line_agentcore"
REGION="us-west-2"
RUNTIME_ID="line_agentcore-dsoclNGvM6"
ROLE_ARN="arn:aws:iam::017820658462:role/AmazonBedrockAgentCoreSDKRuntime-us-west-2-af9dc0ef3b"
S3_BUCKET="bedrock-agentcore-codebuild-sources-017820658462-us-west-2"
S3_PREFIX="line_agentcore/deployment.zip"

echo "========================================"
echo "🚀 LINE AgentCore デプロイ開始"
echo "========================================"

# --- Step 1: AgentCore Runtime にコードをデプロイ ---
echo ""
echo "[1/3] コードを AgentCore Runtime にデプロイ中..."
cd "$SCRIPT_DIR/src"
uv run agentcore deploy --agent "$AGENT_NAME"
echo "✅ コードデプロイ完了"

# --- Step 2: 環境変数を設定（deploy で上書きされるため再設定）---
echo ""
echo "[2/3] 環境変数を設定中..."
aws bedrock-agentcore-control update-agent-runtime \
  --agent-runtime-id "$RUNTIME_ID" \
  --region "$REGION" \
  --agent-runtime-artifact "{\"codeConfiguration\":{\"code\":{\"s3\":{\"bucket\":\"$S3_BUCKET\",\"prefix\":\"$S3_PREFIX\"}},\"runtime\":\"PYTHON_3_12\",\"entryPoint\":[\"opentelemetry-instrument\",\"agent.py\"]}}" \
  --role-arn "$ROLE_ARN" \
  --network-configuration '{"networkMode":"PUBLIC"}' \
  --environment-variables "{
    \"BEDROCK_AGENTCORE_MEMORY_ID\": \"line_agentcore_mem-9qQz574MzM\",
    \"BEDROCK_AGENTCORE_MEMORY_NAME\": \"line_agentcore_mem\",
    \"GATEWAY_URL\": \"https://line-agentcore-searchgateway-chfuqejvmv.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp\",
    \"GATEWAY_PROVIDER_NAME\": \"GatewayAuthProvider\",
    \"HOTPEPPER_API_KEY\": \"f8bfe237f4e839dc\",
    \"AGENTCORE_MEMORY_ID\": \"line_agentcore_mem-9qQz574MzM\",
    \"AGENTCORE_REGION\": \"us-west-2\",
    \"OTEL_TRACES_SAMPLER\": \"always_on\",
    \"DEPLOY_TS\": \"$(date +%Y%m%d_%H%M%S)\"
  }" > /dev/null

echo "  READY になるまで待機中..."
for i in $(seq 1 24); do
  sleep 5
  STATUS=$(aws bedrock-agentcore-control get-agent-runtime \
    --agent-runtime-id "$RUNTIME_ID" \
    --region "$REGION" 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('agentRuntime',d).get('status','?'))")
  echo "  $(date '+%H:%M:%S') $STATUS"
  [ "$STATUS" = "READY" ] && break
done
echo "✅ 環境変数設定完了"

# --- Step 3: Lambda もデプロイ ---
echo ""
echo "[3/3] Lambda (bedrock-agentcore-line-bot) をデプロイ中..."
cd "$SCRIPT_DIR/lambda"
zip -q function.zip lambda_function.py
aws lambda update-function-code \
  --function-name bedrock-agentcore-line-bot \
  --zip-file fileb://function.zip \
  --region "$REGION" > /dev/null
rm -f function.zip
echo "✅ Lambda デプロイ完了"

echo ""
echo "========================================"
echo "🎉 デプロイ完了！LINE でテストしてください"
echo "========================================"
