#
# send -- 일일 개발 블로그 발송 진입점
#
# GitHub Actions 스케줄러가 평일 아침(KST)에 실행하는 스크립트.
# 블로그 글을 가져와 요약하고 Slack에 발송한 뒤 결과를 표준출력에 남긴다.
# 실패 시 exit code 1을 반환해 워크플로가 실패로 표시되도록 한다.
#
# 작성자: 이상윤
#
# 환경변수
#   SLACK_BOT_TOKEN       -- (필수) Bot User OAuth Token
#   GEMINI_API_KEY        -- (선택) 없으면 요약 없이 본문 일부만 발송
#   SLACK_CHANNEL         -- (선택) 기본값 #ai-papers
#   SUBSCRIPTIONS_TABLE   -- (선택) 있으면 구독자별 필터링 DM도 함께 발송
#
# 구성
#   main  -- 수집 -> 변환 -> 채널 발송 -> 구독자 DM 발송 순서로 실행
#
# 변경내역
#   2026-07-24  최초 작성
#   2026-07-26  개발 블로그 모음으로 소스 전면 교체에 맞춰 TOP_N 제거
#               (그날 올라온 글만 모으는 구조라 상한 개념이 없음)
#   2026-07-26  주말 실행 방지 가드 추가. cron을 평일로만 걸어도, 수동
#               실행(workflow_dispatch)이나 cron 지연으로 주말에 걸릴 수
#               있어 코드에서도 한 번 더 막는다
#   2026-07-31  /구독 사용자에게 주제·소스로 필터링한 DM 추가 발송.
#               채널 브로드캐스트는 그대로 두고, 구독자에게만 추가로 감
#

import os
import sys
from datetime import datetime

from dotenv import load_dotenv
from papers import KST, build_blocks, enrich_posts, fetch_recent_posts
from slack_client import post_message
from subscriptions import build_user_digests, fetch_subscriptions

load_dotenv()

CHANNEL = os.environ.get("SLACK_CHANNEL", "#ai-papers")


def _is_weekend_kst():
    return datetime.now(KST).weekday() >= 5  # 5=토, 6=일


# 구독자별로 필터링된 다이제스트를 DM으로 보낸다. 특정 사용자 발송이
# 실패해도(예: 워크스페이스 탈퇴) 나머지 구독자 발송에 영향 없게 사용자
# 단위로 예외를 잡는다.
def _send_subscriber_dms(posts):
    subscriptions = fetch_subscriptions()
    for user_id, filtered_posts in build_user_digests(posts, subscriptions):
        try:
            blocks = build_blocks(filtered_posts)
            post_message(
                channel=user_id,
                blocks=blocks,
                fallback_text=f"오늘의 맞춤 개발 블로그 {len(filtered_posts)}편",
            )
        except Exception as e:
            print(f"[warn] 구독 DM 발송 실패 (user={user_id}): {e}", file=sys.stderr)


# 최근 올라온 블로그 글을 모아 채널에 발송하고, 구독자에게는 필터링된
# DM도 추가로 보낸다. 주말(KST)에는 건너뛴다.
def main():
    # if _is_weekend_kst():
    #     print("주말입니다. 발송을 건너뜁니다.")
    #     return

    posts = fetch_recent_posts()
    enrich_posts(posts)
    blocks = build_blocks(posts)
    fallback = (
        f"오늘의 개발 블로그 {len(posts)}편" if posts else "오늘의 개발 블로그: 새 글 없음"
    )
    ts = post_message(channel=CHANNEL, blocks=blocks, fallback_text=fallback)
    print(f"발송 완료: {len(posts)}편 (ts={ts})")

    _send_subscriber_dms(posts)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(1)