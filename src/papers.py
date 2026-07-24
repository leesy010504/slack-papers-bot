"""HF Daily Papers 수집 · 요약 · Slack 블록 변환.

발송 진입점(send.py)과 분리해 두었다.
나중에 슬래시 커맨드 등 다른 진입점이 생겨도 이 모듈을 그대로 재사용한다.
"""

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


def fetch_papers(n=3):
    """피드에서 최신 논문 n편을 가져온다."""
    feed = feedparser.parse(FEED_URL)
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"피드 파싱 실패: {feed.bozo_exception}")
    return feed.entries[:n]


def summarize(title, abstract):
    """초록을 한국어로 요약한다. API 키가 없으면 원문을 잘라서 반환."""
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
        # 요약이 실패해도 발송 자체는 계속한다
        print(f"[warn] 요약 실패 ({title[:40]}...): {e}")
        return abstract[:300].strip()


def build_blocks(entries):
    """논문 목록을 Slack Block Kit 형식으로 변환한다."""
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