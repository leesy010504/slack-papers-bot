#
# send -- 일일 논문 발송 진입점
#
# GitHub Actions 스케줄러가 매일 아침 실행하는 스크립트.
# 논문을 가져와 요약하고 Slack에 발송한 뒤 결과를 표준출력에 남긴다.
# 실패 시 exit code 1을 반환해 워크플로가 실패로 표시되도록 한다.
#
# 작성자: 이상윤
#
# 환경변수
#   SLACK_BOT_TOKEN    -- (필수) Bot User OAuth Token
#   ANTHROPIC_API_KEY  -- (선택) 없으면 요약 없이 초록 일부만 발송
#   SLACK_CHANNEL      -- (선택) 기본값 #ai-papers
#   TOP_N              -- (선택) 발송할 논문 수, 기본값 3
#
# 구성
#   main  -- 수집 -> 변환 -> 발송 순서로 실행
#
# 변경내역
#   2026-07-24  최초 작성
#

import os

import feedparser
import requests

FEED_URL = "https://papers.takara.ai/api/feed"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-6"

SUMMARY_PROMPT = (
    "다음 논문 초록을 한국어 3줄로 요약해줘. "
    "기술 용어(attention, embedding 등)는 영어 그대로 두고, "
    "불릿 없이 문장으로만 써줘.\n\n"
    "제목: {title}\n초록: {abstract}"
)

# 피드에서 최신 논문 n편을 가져온다.
def fetch_papers(n=3):
    feed = feedparser.parse(FEED_URL)
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"피드 파싱 실패: {feed.bozo_exception}")
    return feed.entries[:n]

# 초록을 한국어로 요약한다. API 키가 없으면 원문을 잘라서 반환한다.
def summarize(title, abstract):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return abstract[:300].strip()

    try:
        res = requests.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": MODEL,
                "max_tokens": 300,
                "messages": [{
                    "role": "user",
                    "content": SUMMARY_PROMPT.format(
                        title=title, abstract=abstract
                    ),
                }],
            },
            timeout=60,
        )
        res.raise_for_status()
        return res.json()["content"][0]["text"].strip()
    except Exception as e:
        print(f"[warn] 요약 실패 ({title[:40]}...): {e}")
        return abstract[:300].strip()

# 논문 목록을 Slack Block Kit 형식으로 변환한다.
def build_blocks(entries):
    blocks = [{
        "type": "header",
        "text": {"type": "plain_text", "text": "오늘의 AI 논문"},
    }]

    for entry in entries:
        summary = summarize(entry.title, entry.get("summary", ""))
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*<{entry.link}|{entry.title}>*\n{summary}",
            },
        })
        blocks.append({"type": "divider"})

    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": "출처: Hugging Face Daily Papers",
        }],
    })
    return blocks