#
# slack_client -- Slack 메시지 발송
#
# Slack Web API의 chat.postMessage를 호출해 지정 채널에 메시지를 보낸다.
# Bot Token(xoxb-) 방식을 사용하며, 응답으로 받은 메시지 ts를 반환해
# 후속 스레드 발송에 활용할 수 있다.
#
# 작성자: 이상윤
#
# 구성
#   post_message  -- chat.postMessage 호출 및 응답 검증
#
# 변경내역
#   2026-07-24  최초 작성
#

import os

import requests

POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"

# Slack 채널에 Block Kit 메시지를 발송한다.
def post_message(channel, blocks, fallback_text, thread_ts=None):

    token = os.environ["SLACK_BOT_TOKEN"]

    payload = {
        "channel": channel,
        "text": fallback_text,
        "blocks": blocks,
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts

    res = requests.post(
        POST_MESSAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json=payload,
        timeout=30,
    )
    res.raise_for_status()

    data = res.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack API 오류: {data.get('error')} / {data}")

    return data["ts"]