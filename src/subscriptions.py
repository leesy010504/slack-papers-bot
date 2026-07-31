#
# subscriptions -- 사용자별 구독 정보 조회 및 글 필터링
#
# subscribe/app.py(Slack /구독 명령을 처리하는 Lambda)가 DynamoDB에 쌓아둔
# 구독 정보를 읽어와, 사용자별로 관심 주제/소스에 맞는 글만 걸러낸다.
# send.py가 채널 브로드캐스트에 더해 구독자별 DM을 보낼 때 사용한다.
#
# 환경변수
#   SUBSCRIPTIONS_TABLE  -- (선택) 구독 정보가 저장된 DynamoDB 테이블 이름.
#                           없으면 구독 기능 없이(빈 목록) 동작한다.
#
# 작성자: 이상윤
#

import os

import boto3

# papers.py SOURCES의 데보션 관련 소스(블로그/트렌드/뉴스)는 전부 같은
# 소스라 구독 단위에서는 "데보션" 하나로 묶는다. subscribe/app.py의
# SOURCES 정의와 맞춰야 한다.
DEVOCEAN_PREFIX = "데보션"
ALL_MARKER = "전체"


def _source_of(post):
    source = post["source"]
    return DEVOCEAN_PREFIX if source.startswith(DEVOCEAN_PREFIX) else source


# DynamoDB에 저장된 모든 구독을 읽어온다. 저장 키가 "team_id#user_id"
# 형태라 DM 발송에 필요한 user_id만 추려 반환한다. 테이블이 설정 안
# 되어 있으면(로컬 실행 등) 빈 목록을 반환해 구독 기능 없이도 동작하게 한다.
def fetch_subscriptions():
    table_name = os.environ.get("SUBSCRIPTIONS_TABLE")
    if not table_name:
        return []

    table = boto3.resource("dynamodb").Table(table_name)
    items = []
    scan_kwargs = {}
    while True:
        res = table.scan(**scan_kwargs)
        items.extend(res.get("Items", []))
        if "LastEvaluatedKey" not in res:
            break
        scan_kwargs["ExclusiveStartKey"] = res["LastEvaluatedKey"]

    subscriptions = []
    for item in items:
        user_id = item["user_id"].split("#")[-1]
        topics = set(item.get("topics", []))
        sources = set(item.get("sources", []))
        if topics or sources:
            subscriptions.append({"user_id": user_id, "topics": topics, "sources": sources})
    return subscriptions


# 주제 조건 판정. 사용자가 주제를 지정 안 했거나, 카테고리가 없는 뉴스
# 다이제스트라 애초에 평가할 수 없으면 "의견 없음"(None)으로 빠진다.
def _topic_matches(post, topics):
    if not topics:
        return None
    if ALL_MARKER in topics:
        return True
    if post["region"] == "news":
        return None
    return post.get("category") in topics


# 소스 조건 판정. 사용자가 소스를 지정 안 했으면 "의견 없음"(None)으로 빠진다.
def _source_matches(post, sources):
    if not sources:
        return None
    if ALL_MARKER in sources:
        return True
    return _source_of(post) in sources


# 글 하나가 사용자의 구독 조건(주제·소스)에 맞는지 판단한다. 두 조건은
# OR로 묶는다 -- "주제 AI"와 "소스 Cloudflare"를 함께 구독했다면 AI 글이든
# Cloudflare 글이든 받는다(AND로 좁히면 "Cloudflare가 쓴 AI 글"만 남아
# 사실상 거의 안 옴). 평가 가능한 조건이 하나도 없으면(둘 다 미지정이거나,
# 주제만 지정했는데 카테고리 없는 뉴스인 경우) 기본적으로 통과시킨다.
def _matches(post, topics, sources):
    results = [
        r for r in (_topic_matches(post, topics), _source_matches(post, sources))
        if r is not None
    ]
    return True if not results else any(results)


# 구독 목록에 맞춰 (slack_user_id, 필터링된 posts) 쌍의 목록을 만든다.
# 필터링 결과가 없는 사용자(오늘 해당 글이 없음)는 목록에서 제외한다.
def build_user_digests(posts, subscriptions):
    digests = []
    for sub in subscriptions:
        filtered = [p for p in posts if _matches(p, sub["topics"], sub["sources"])]
        if filtered:
            digests.append((sub["user_id"], filtered))
    return digests
