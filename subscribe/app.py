#
# app -- Slack 슬래시 커맨드(/구독) 처리 Lambda
#
# `/구독 주제 {값}`, `/구독 소스 {값}` 형태의 요청을 받아 DynamoDB에
# 사용자별 구독 목록을 누적 저장한다. `/구독 해제 주제 {값}`으로 구독을
# 뺄 수 있고, 값은 공백이나 쉼표로 여러 개를 한 번에 넘길 수 있다.
# Slack Signing Secret으로 요청 출처를 검증한다.
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
SOURCES = [
    "당근", "네이버 D2", "우아한형제들", "데보션", "카카오", "올리브영", "KT Cloud",
    "Cloudflare", "Simon Willison", "라인야후", "전체",
]
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


# 사용자 입력 text("주제 ai", "해제 소스 당근,카카오" 등)를
# (action, kind, raw_values)로 나눈다. 맨 앞 토큰이 "해제"면 구독 해제,
# 아니면 구독 등록으로 본다. 형식이 안 맞으면 None을 반환해 사용법
# 안내로 이어지게 한다.
def _parse_command_text(text):
    parts = text.strip().split(maxsplit=1)
    if len(parts) != 2:
        return None
    first, rest = parts[0].strip(), parts[1].strip()

    action = "add"
    kind = first
    if first == "해제":
        action = "remove"
        sub_parts = rest.split(maxsplit=1)
        if len(sub_parts) != 2:
            return None
        kind, rest = sub_parts[0].strip(), sub_parts[1].strip()

    if kind not in VALID_KINDS or not rest:
        return None
    return action, kind, rest


# "AI 보안", "AI,보안", "네이버 D2,카카오" 처럼 섞어 쓴 값을 나눈다.
# 쉼표가 있으면 쉼표로만 나눈다(소스명 중 "네이버 D2", "KT Cloud"처럼
# 값 자체에 공백이 들어간 게 있어서, 공백 분리와 섞으면 깨진다). 쉼표가
# 없으면 문자열 전체를 값 하나로 먼저 시도해 공백 포함 값도 살리고,
# 그게 유효한 값이 아닐 때만 공백으로 나눈다.
def _split_values(raw_values, attr):
    if "," in raw_values:
        return [v.strip() for v in raw_values.split(",") if v.strip()]
    if raw_values.lower() in VALID_VALUES[attr]:
        return [raw_values]
    return raw_values.split()


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
        f"`/구독 소스 {{{'|'.join(SOURCES)}}}` (쉼표나 공백으로 여러 개 가능)\n"
        f"해제: `/구독 해제 주제 {{값}}` 또는 `/구독 해제 소스 {{값}}`"
    )

    parsed = _parse_command_text(text)
    if parsed is None:
        return _respond(usage)
    action, kind, raw_values = parsed
    attr = VALID_KINDS[kind]

    values, invalid = [], []
    for raw_value in _split_values(raw_values, attr):
        value = VALID_VALUES[attr].get(raw_value.lower())
        (invalid if value is None else values).append(raw_value if value is None else value)

    if invalid:
        return _respond(f"`{', '.join(invalid)}`는 지원하지 않는 값이에요.\n{usage}")
    if not values:
        return _respond(usage)

    table = dynamodb.Table(os.environ["TABLE_NAME"])
    key = f"{team_id}#{user_id}"
    verb = "ADD" if action == "add" else "DELETE"
    item = table.update_item(
        Key={"user_id": key},
        UpdateExpression=f"{verb} {attr} :v",
        ExpressionAttributeValues={":v": set(values)},
        ReturnValues="ALL_NEW",
    )["Attributes"]

    topics = sorted(item.get("topics", []))
    sources = sorted(item.get("sources", []))
    action_label = "등록" if action == "add" else "해제"
    return _respond(
        f"구독 {action_label} 완료: {kind} `{', '.join(values)}`\n"
        f"현재 주제 구독: {', '.join(topics) or '없음'}\n"
        f"현재 소스 구독: {', '.join(sources) or '없음'}"
    )
