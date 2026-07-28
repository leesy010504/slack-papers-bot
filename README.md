# Daily Papers Bot

개발 블로그(당근, Cloudflare, 네이버 D2, 우아한형제들, Simon Willison, Hugging Face,
데보션 등) 최신 글을 한국어로 요약해 평일 아침(KST) Slack에 보냅니다.

## 구조

| 파일 | 역할 |
| --- | --- |
| `papers.py` | 피드 수집, 요약, Block Kit 변환 |
| `slack_client.py` | Slack 발송 |
| `send.py` | cron 진입점 |

## 사용 방법

### 1. Slack 앱

1. https://api.slack.com/apps → Create New App → **Blank app**
2. **OAuth & Permissions** → Bot Token Scopes에 `chat:write` 추가
3. **Install to Workspace** → `xoxb-`로 시작하는 Bot User OAuth Token 복사
4. 대상 채널에서 `/invite @앱이름` (빠뜨리면 `not_in_channel` 오류)

### 2. GitHub Secrets

Settings → Secrets and variables → Actions

| 이름 | 값 |
| --- | --- |
| `SLACK_BOT_TOKEN` | `xoxb-...` |
| `GEMINI_API_KEY` | Gemini API 키 (없으면 요약 없이 본문 일부 발송) |

## 로컬 실행

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements

export SLACK_BOT_TOKEN="xoxb-..."
export GEMINI_API_KEY="..."
export SLACK_CHANNEL="#ai-papers"

python src/send.py
```

## 배포 후 확인

Actions 탭 → `Daily Papers to Slack` → **Run workflow**로 수동 실행해 검증한다.

## 참고

- 스케줄은 기본 브랜치에서만 동작합니다.
- Actions cron은 정시를 보장하지 않아 5~30분 지연될 수 있습니다.