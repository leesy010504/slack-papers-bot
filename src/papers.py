#
# papers -- HF Daily Papers 수집 및 Slack 메시지 변환
#
# Hugging Face Daily Papers의 RSS 피드에서 최신 논문을 가져와 Gemini API로
# 한국어 요약을 생성하고, Slack Block Kit 형식으로 변환한다.
# 발송 진입점(send.py)과 분리해 두어 다른 진입점에서도 재사용할 수 있다.
#
# 작성자: 이상윤
#
# 환경변수
#   GEMINI_API_KEY  -- (선택) 없으면 요약 없이 초록 일부만 반환
#   GEMINI_MODEL    -- (선택) 기본값 gemini-flash-lite-latest
#
# 구성
#   fetch_papers    -- RSS 피드에서 최신 논문 n편 조회
#   summarize       -- 본문을 4단 구조(요약/의미/포인트/용어)로 변환
#   format_summary  -- 모델 출력을 Slack mrkdwn 서식으로 정리
#   build_blocks    -- 논문 목록 -> Slack Block Kit 변환
#
# 변경내역
#   2026-07-24  최초 작성
#   2026-07-24  요약 엔진을 Claude에서 Gemini로 교체
#   2026-07-24  가독성 개선 (프롬프트 조정, 문장 단위 줄바꿈 추가)
#   2026-07-24  요약 형식을 '한 줄 + 독자 안내'로 변경
#   2026-07-24  독자층 편차를 고려해 요약/의미/포인트/용어 4단 구조로 확장
#

import os
import re

import feedparser
import requests

FEED_URL = "https://papers.takara.ai/api/feed"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# 모델 세대가 자주 교체되므로 버전 없는 -latest 별칭을 기본으로 둔다.
# 404나 429 limit:0 이 뜨면 사용 가능한 모델 목록을 확인할 것
# https://ai.google.dev/gemini-api/docs/models
DEFAULT_MODEL = "gemini-flash-lite-latest"

# 독자층이 전공자·비전공자·AI 입문자로 섞여 있다.
# 한 메시지 안에 깊이가 다른 층을 쌓아 각자 필요한 만큼만 읽게 한다.
#   1줄 요약   -> 훑고 지나갈지 판단 (전원)
#   왜 중요한가 -> 맥락 (기초가 부족한 사람)
#   용어 풀이   -> 막히는 단어 해소 (비전공자)
SUMMARY_PROMPT = (
    "다음 글을 아래 네 항목으로 정리해줘. "
    "독자는 개발자지만 AI 배경지식이 없을 수도 있다.\n\n"
    "[요약]\n무엇을 하는 기술/글인지 한 문장 (70자 이내, 전문용어 없이)\n\n"
    "[의미]\n왜 중요한지 두 문장. "
    "'기존에는 ~였다'로 시작해 무엇이 달라지는지 설명\n\n"
    "[포인트]\n기억할 내용 2가지. 각 40자 이내로 한 줄씩\n\n"
    "[용어]\n핵심 용어 2개. '용어 - 한 줄 설명' 형식으로 한 줄씩. "
    "용어는 영어 원어 그대로, 설명만 한국어로\n\n"
    "규칙\n"
    "- 대괄호 머리말([요약] 등)을 반드시 그대로 포함할 것\n"
    "- 각 줄에 번호나 불릿 기호를 붙이지 말 것\n"
    "- [요약]은 비전공자도 이해하도록. 어려우면 비유를 써라\n"
    "- 지정한 네 항목 외에 다른 말은 하지 마\n\n"
    "제목: {title}\n내용: {abstract}"
)


# 피드에서 최신 논문 n편을 가져온다.
def fetch_papers(n=3):
    feed = feedparser.parse(FEED_URL)
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"피드 파싱 실패: {feed.bozo_exception}")
    return feed.entries[:n]


# 초록을 한국어로 요약한다. 키가 없거나 호출이 실패하면 원문을 잘라서 반환한다.
def summarize(title, abstract):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return abstract[:300].strip()

    # 모듈 로드 시점이 아니라 호출 시점에 읽는다.
    # 상수로 두면 load_dotenv()보다 import가 먼저 실행될 때 기본값이 박힌다.
    model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    url = f"{GEMINI_BASE}/{model}:generateContent"

    try:
        res = requests.post(
            url,
            headers={
                "x-goog-api-key": key,
                "Content-Type": "application/json",
            },
            json={
                "contents": [{
                    "parts": [{
                        "text": SUMMARY_PROMPT.format(
                            title=title, abstract=abstract
                        )
                    }]
                }],
                "generationConfig": {"maxOutputTokens": 800},
            },
            timeout=60,
        )
        res.raise_for_status()
        data = res.json()

        # 안전 필터 등으로 후보가 비어 올 수 있다
        candidates = data.get("candidates")
        if not candidates:
            raise RuntimeError(f"응답에 candidates 없음: {data}")

        return candidates[0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print(f"[warn] 요약 실패 ({title[:40]}...): {e}")
        return abstract[:300].strip()


# 모델 출력을 Slack mrkdwn으로 변환한다.
# [요약]/[의미]/[포인트]/[용어] 섹션을 각각 다른 서식으로 눌러 시선 순위를 만든다.
# 모델이 형식을 어기면 원문을 그대로 반환해 메시지가 깨지지 않게 한다.
def format_summary(text):
    sections = {}
    current = None
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        matched = re.match(r"^\[(요약|의미|포인트|용어)\]", line)
        if matched:
            current = matched.group(1)
            sections[current] = []
            rest = line[matched.end():].strip()
            if rest:
                sections[current].append(rest)
        elif current:
            sections[current].append(line)

    if "요약" not in sections:
        return " ".join(text.split())

    parts = [" ".join(sections["요약"])]

    if sections.get("의미"):
        parts.append("_" + " ".join(sections["의미"]) + "_")

    if sections.get("포인트"):
        parts.append(
            "\n".join("  • " + ln.lstrip("-•* ") for ln in sections["포인트"])
        )

    if sections.get("용어"):
        terms = []
        for ln in sections["용어"]:
            ln = ln.lstrip("-•* ")
            if " - " in ln:
                term, desc = ln.split(" - ", 1)
                terms.append(f"  `{term.strip()}` {desc.strip()}")
            else:
                terms.append(f"  {ln}")
        parts.append("\n".join(terms))

    return "\n\n".join(parts)


# 논문 목록을 Slack Block Kit 형식으로 변환한다.
def build_blocks(entries):
    blocks = [{
        "type": "header",
        "text": {"type": "plain_text", "text": "오늘의 AI 논문"},
    }]

    for entry in entries:
        summary = format_summary(
            summarize(entry.title, entry.get("summary", ""))
        )
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