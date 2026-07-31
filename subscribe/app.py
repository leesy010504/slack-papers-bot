#
# app -- Slack 슬래시 커맨드(/구독) 처리 Lambda
#
# `/구독 주제 {값}`, `/구독 소스 {값}` 형태의 요청을 받아 DynamoDB에
# 사용자별 구독 목록을 누적 저장한다. Slack Signing Secret으로 요청
# 출처를 검증한다.
#
# 환경변수
#   SLACK_SIGNING_SECRET  -- (필수) Slack 앱 Basic Information의 Signing Secret
#   TABLE_NAME             -- (필수) 구독 정보를 저장할 DynamoDB 테이블 이름
#

import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qs

import boto3

VALID_KINDS = {"주제": "topics", "소스": "sources"}

# papers.py의 CATEGORIES / SOURCES와 맞춘 값 + "전체". 데보션 블로그/트렌드/뉴스는
# 전부 같은 소스라 구독 단위에서는 "데보션" 하나로 묶는다.
TOPICS = ["보안", "AI", "인프라", "백엔드", "프론트엔드", "기타", "전체"]
SOURCES = ["당근", "네이버 D2", "우아한형제들", "데보션", "Cloudflare", "Simon Willison", "전체"]
VALID_VALUES = {
    "topics": {v.lower(): v for v in TOPICS},
    "sources": {v.lower(): v for v in SOURCES},
}

dynamodb = boto3.resource("dynamodb")


def _verify_slack_signature(headers, body):
    signing_secret = os.environ["SLACK_SIGNING_SECRET"]
    headers = {k.lower(): v for k, v in headers.items()}
    timestamp = headers.get("x-slack-request-timestamp", "")
    signature = headers.get("x-slack-signature", "")

    # 재전송 공격 방지: 5분 넘게 지난 요청은 버린다.
    # 헤더가 없거나 숫자가 아니면(위조/이상 요청) 검증 실패로 처리한다.
    try:
        if abs(time.time() - int(timestamp)) > 60 * 5:
            return False
    except ValueError:
        return False

    basestring = f"v0:{timestamp}:{body}".encode()
    digest = hmac.new(signing_secret.encode(), basestring, hashlib.sha256).hexdigest()
    expected = f"v0={digest}"
    return hmac.compare_digest(expected, signature)


def _respond(text):
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"response_type": "ephemeral", "text": text}),
    }


# 사용자 입력 text("주제 ai" 등)를 (kind, value)로 나눈다.
# 형식이 안 맞으면 None을 반환해 사용법 안내로 이어지게 한다.
def _parse_command_text(text):
    parts = text.strip().split(maxsplit=1)
    if len(parts) != 2:
        return None
    kind, value = parts[0].strip(), parts[1].strip()
    if kind not in VALID_KINDS or not value:
        return None
    return kind, value


def lambda_handler(event, context):
    body = event.get("body", "")
    if event.get("isBase64Encoded"):
        import base64
        body = base64.b64decode(body).decode()

    if not _verify_slack_signature(event.get("headers", {}), body):
        return {"statusCode": 401, "body": "invalid signature"}

    form = {k: v[0] for k, v in parse_qs(body).items()}
    team_id = form.get("team_id", "")
    user_id = form.get("user_id", "")
    text = form.get("text", "")

    usage = (
        f"사용법: `/구독 주제 {{{'|'.join(TOPICS)}}}` 또는 "
        f"`/구독 소스 {{{'|'.join(SOURCES)}}}`"
    )

    parsed = _parse_command_text(text)
    if parsed is None:
        return _respond(usage)
    kind, raw_value = parsed
    attr = VALID_KINDS[kind]

    value = VALID_VALUES[attr].get(raw_value.lower())
    if value is None:
        return _respond(f"`{raw_value}`는 지원하지 않는 값이에요.\n{usage}")

    table = dynamodb.Table(os.environ["TABLE_NAME"])
    key = f"{team_id}#{user_id}"
    item = table.update_item(
        Key={"user_id": key},
        UpdateExpression=f"ADD {attr} :v",
        ExpressionAttributeValues={":v": {value}},
        ReturnValues="ALL_NEW",
    )["Attributes"]

    topics = sorted(item.get("topics", []))
    sources = sorted(item.get("sources", []))
    return _respond(
        f"구독 등록 완료: {kind} `{value}`\n"
        f"현재 주제 구독: {', '.join(topics) or '없음'}\n"
        f"현재 소스 구독: {', '.join(sources) or '없음'}"
    )
