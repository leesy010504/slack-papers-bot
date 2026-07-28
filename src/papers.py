#
# papers -- 개발 블로그 모음 수집 및 Slack 메시지 변환
#
# 여러 개발 블로그의 RSS/Atom 피드에서 최근 올라온 글만 모아 Gemini API로
# 짧게 요약하고, Slack Block Kit 형식으로 변환한다.
# 발송 진입점(send.py)과 분리해 두어 다른 진입점에서도 재사용할 수 있다.
#
# 작성자: 이상윤
#
# 환경변수
#   GEMINI_API_KEY  -- (선택) 없으면 요약 없이 본문 일부만 반환
#   GEMINI_MODEL    -- (선택) 기본값 gemini-flash-lite-latest
#
# 구성
#   fetch_recent_posts  -- 소스별 피드에서 최근 올라온 글만 수집
#   summarize            -- 글 본문을 Gemini API로 한국어 요약
#   format_summary        -- 요약문을 문장 단위 불릿으로 정리
#   build_blocks           -- 수집 결과 -> Slack Block Kit 변환
#
# 변경내역
#   2026-07-24  최초 작성 (HF Daily Papers 기반)
#   2026-07-24  요약 엔진을 Claude에서 Gemini로 교체
#   2026-07-24  가독성 개선 (프롬프트 조정, 문장 단위 줄바꿈 추가)
#   2026-07-24  요약 형식을 '한 줄 + 독자 안내'로 변경
#   2026-07-24  독자층 편차를 고려해 요약/의미/포인트/용어 4단 구조로 확장
#   2026-07-26  논문 대신 개발 블로그 모음으로 소스 전면 교체, 4단 구조는
#               블로그 글엔 과해서 문장 단위 불릿 요약으로 되돌림
#   2026-07-26  평일 아침에만 실행하는 걸 전제로, KST 날짜 일치 대신 시차를
#               고려한 lookback 윈도우로 수집 기준 변경 (주말 시차 유실 방지)
#

import html
import os
import re
from datetime import datetime, timedelta, timezone

import feedparser
import requests

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# 모델 세대가 자주 교체되므로 버전 없는 -latest 별칭을 기본으로 둔다.
# 404나 429 limit:0 이 뜨면 사용 가능한 모델 목록을 확인할 것
# https://ai.google.dev/gemini-api/docs/models
DEFAULT_MODEL = "gemini-flash-lite-latest"

KST = timezone(timedelta(hours=9))

# 데보션은 뉴스/블로그/AI블로그 게시판이 나뉘어 있지만 RSS는 하나로 통합되어
# 내려온다. "일일 Tech News & Blog (26.07.24)" 형식의 다이제스트 글만
# 제목으로 구분할 수 있어 그 글만 따로 "데보션 뉴스"로 분리한다.
DEVOCEAN_NEWS_TITLE_RE = re.compile(r"^일일 Tech News")

SOURCES = [
    {"name": "당근", "feed": "https://medium.com/feed/daangn", "url": "https://medium.com/daangn"},
    {"name": "Cloudflare", "feed": "https://blog.cloudflare.com/rss/", "url": "https://blog.cloudflare.com/"},
    {"name": "네이버 D2", "feed": "https://d2.naver.com/d2.atom", "url": "https://d2.naver.com/home"},
    {"name": "우아한형제들", "feed": "https://techblog.woowahan.com/feed/", "url": "https://techblog.woowahan.com/"},
    {"name": "Simon Willison", "feed": "https://simonwillison.net/atom/everything/", "url": "https://simonwillison.net/"},
    {"name": "Hugging Face", "feed": "https://huggingface.co/blog/feed.xml", "url": "https://huggingface.co/blog"},
    {"name": "데보션", "feed": "https://devocean.sk.com/blog/rss.do", "url": "https://devocean.sk.com/blog/index.do?p=BLOG"},
]

# Slack 메시지당 블록 수 상한(50)에 안전 마진을 두고, 소스가 늘어나거나
# 특정 소스가 그날 유독 많이 올려도 메시지가 깨지지 않게 한다.
MAX_POSTS = 20

SUMMARY_PROMPT = (
    "다음 개발 블로그 글을 한국어 2~3문장으로 요약해줘. "
    "이 글을 읽어야 할 이유가 드러나게 핵심만 담고, "
    "불릿 없이 문장으로만 써줘. "
    "요약문만 출력하고 다른 말은 하지 마.\n\n"
    "제목: {title}\n내용: {content}"
)


# 데보션 통합 피드의 글 제목으로 "데보션 뉴스"와 "데보션 블로그"를 구분한다.
def _resolve_source_name(source_name, title):
    if source_name == "데보션" and DEVOCEAN_NEWS_TITLE_RE.match(title.strip()):
        return "데보션 뉴스"
    if source_name == "데보션":
        return "데보션 블로그"
    return source_name


# 피드 엔트리의 발행/수정 시각을 UTC datetime으로 변환한다.
# published_parsed가 없는 피드(예: 네이버 D2)는 updated_parsed로 대체한다.
def _entry_datetime_utc(entry):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=timezone.utc)


# 이번 실행에서 몇 시간 전까지 거슬러 올라가 글을 모을지 정한다.
# 평일 아침에만 실행하므로 보통 지난 24시간이면 충분하지만, 월요일은
# 주말 동안 쌓인 것까지 봐야 한다. 특히 미국 등 KST와 시차가 큰 소스는
# 현지 금요일 오후~저녁 글이 UTC/KST로는 토요일로 넘어가곤 하는데, 우리는
# 주말에 실행하지 않으므로 이 구간을 안 넣으면 그 글을 영영 못 잡는다.
def _lookback_hours(now_kst):
    return 72 if now_kst.weekday() == 0 else 24  # 0 = 월요일


# 엔트리 본문을 뽑아 태그를 제거한 평문으로 반환한다.
# content -> summary -> description 순으로 있는 것을 쓰고, 셋 다 없으면
# 빈 문자열을 반환한다(Hugging Face 블로그 피드가 이 경우에 해당).
def _extract_text(entry):
    content = entry.get("content")
    if content:
        raw = content[0].get("value", "")
    else:
        raw = entry.get("summary") or entry.get("description") or ""
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return " ".join(text.split())[:4000]


# 등록된 모든 소스에서 최근(평일 실행 간격 기준) 올라온 글만 모아 반환한다.
def fetch_recent_posts():
    now_kst = datetime.now(KST)
    window_start = datetime.now(timezone.utc) - timedelta(hours=_lookback_hours(now_kst))
    posts = []

    for source in SOURCES:
        feed = feedparser.parse(source["feed"])
        if feed.bozo and not feed.entries:
            print(f"[warn] 피드 파싱 실패 ({source['name']}): {feed.bozo_exception}")
            continue

        for entry in feed.entries:
            entry_dt = _entry_datetime_utc(entry)
            if entry_dt is None or entry_dt < window_start:
                continue
            title = entry.get("title", "(제목 없음)")
            posts.append({
                "source": _resolve_source_name(source["name"], title),
                "title": title,
                "link": entry.get("link", ""),
                "text": _extract_text(entry),
            })

    return posts[:MAX_POSTS]


# 본문을 한국어로 요약한다. 본문이 없으면 요약을 만들지 않고 빈 문자열을
# 반환한다(제목/링크만 있는 카드로 표시됨). 키가 없거나 호출이 실패하면
# 본문 일부를 잘라서 반환한다.
def summarize(title, text):
    if not text:
        return ""

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return text[:200].strip()

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
                        "text": SUMMARY_PROMPT.format(title=title, content=text)
                    }]
                }],
                "generationConfig": {"maxOutputTokens": 300},
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
        return text[:200].strip()


# 요약문을 문장 단위로 나눠 불릿으로 묶는다.
# 한국어 요약은 대부분 "~다."로 끝나므로 그 뒤에서 자르면 문장 경계와 맞는다.
def format_summary(text):
    if not text:
        return ""
    text = " ".join(text.split())
    sentences = re.split(r"(?<=다\.)\s+", text)
    return "\n".join(f"• {s}" for s in sentences if s)


# 어떤 블로그를 모니터링 중인지 안내하는 context 블록.
# 글이 있는 날/없는 날 모두 붙여서, 채널에 새로 들어온 사람도 이 봇이
# 어떤 소스를 커버하는지 바로 알 수 있게 한다.
def _sources_context_block():
    return {
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": "모니터링 중: " + " · ".join(
                f"<{s['url']}|{s['name']}>" for s in SOURCES
            ),
        }],
    }


# 수집한 글 목록을 Slack Block Kit 형식으로 변환한다.
# 오늘 올라온 글이 하나도 없으면 안내 문구만 담은 블록을 반환한다.
def build_blocks(posts):
    blocks = [{
        "type": "header",
        "text": {"type": "plain_text", "text": "오늘의 개발 블로그"},
    }]

    if not posts:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "오늘은 새로 올라온 글이 없습니다."},
        })
        blocks.append(_sources_context_block())
        return blocks

    n_sources = len({post["source"] for post in posts})
    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": f"오늘 {n_sources}개 소스에서 {len(posts)}편",
        }],
    })

    for post in posts:
        summary = format_summary(summarize(post["title"], post["text"]))
        header = f"*[{post['source']}] <{post['link']}|{post['title']}>*"
        text = f"{header}\n\n{summary}" if summary else header
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": text},
        })
        blocks.append({"type": "divider"})

    blocks.append(_sources_context_block())
    return blocks
