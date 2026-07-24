"""
send -- 일일 논문 발송 진입점

GitHub Actions 스케줄러가 매일 아침 실행하는 스크립트.
논문을 가져와 요약하고 Slack에 발송한 뒤 결과를 표준출력에 남긴다.
실패 시 exit code 1을 반환해 워크플로가 실패로 표시되도록 한다.

작성자: 이상윤

환경변수
  SLACK_BOT_TOKEN    -- (필수) Bot User OAuth Token
  ANTHROPIC_API_KEY  -- (선택) 없으면 요약 없이 초록 일부만 발송
  SLACK_CHANNEL      -- (선택) 기본값 #ai-papers
  TOP_N              -- (선택) 발송할 논문 수, 기본값 3

구성
  main  -- 수집 -> 변환 -> 발송 순서로 실행

변경내역
  2026-07-24  최초 작성
"""

import os
import sys

from papers import build_blocks, fetch_papers
from slack_client import post_message

CHANNEL = os.environ.get("SLACK_CHANNEL", "#ai-papers")
TOP_N = int(os.environ.get("TOP_N", "3"))


# 논문을 수집해 Slack에 발송한다.
def main():
    
    entries = fetch_papers(TOP_N)
    if not entries:
        print("가져온 논문이 없습니다. 발송을 건너뜁니다.")
        return

    blocks = build_blocks(entries)
    ts = post_message(
        channel=CHANNEL,
        blocks=blocks,
        fallback_text=f"오늘의 AI 논문 {len(entries)}편",
    )
    print(f"발송 완료: {len(entries)}편 (ts={ts})")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(1)