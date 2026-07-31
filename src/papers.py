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

# region으로 국내/해외를 나눠 fetch_recent_posts에서 국내 글을 먼저 모으게 한다.
# 데보션은 BLOG/NEWS/DEVOTEE 세 탭으로 나뉘는데, rss.do가 커버하는 건 BLOG
# 탭(개인 작성 글)뿐이라 여기 등록한다. NEWS/DEVOTEE는 RSS에 아예 없어
# DEVOCEAN_SCRAPED_TABS로 목록 페이지를 직접 긁어온다.
SOURCES = [
    # 국내
    {"name": "당근", "region": "domestic", "feed": "https://medium.com/feed/daangn", "url": "https://medium.com/daangn"},
    {"name": "네이버 D2", "region": "domestic", "feed": "https://d2.naver.com/d2.atom", "url": "https://d2.naver.com/home"},
    {"name": "우아한형제들", "region": "domestic", "feed": "https://techblog.woowahan.com/feed/", "url": "https://techblog.woowahan.com/"},
    {"name": "데보션 블로그", "region": "domestic", "feed": "https://devocean.sk.com/blog/rss.do", "url": "https://devocean.sk.com/blog/index.do?p=BLOG"},
    # 해외
    {"name": "Cloudflare", "region": "international", "feed": "https://blog.cloudflare.com/rss/", "url": "https://blog.cloudflare.com/"},
    {"name": "Simon Willison", "region": "international", "feed": "https://simonwillison.net/atom/everything/", "url": "https://simonwillison.net/"},
]

# Slack 메시지당 블록 수 상한(50)에 안전 마진을 두고, 소스가 늘어나거나
# 특정 소스가 그날 유독 많이 올려도 메시지가 깨지지 않게 한다.
MAX_POSTS = 20

SUMMARY_PROMPT = (
    "다음 개발 블로그 글의 핵심 내용을 한국어 한 문장으로 요약해줘. "
    "존댓말로 쓰고 '~글입니다'처럼 명사형 어미로 끝내줘. "
    "요약문만 출력하고 다른 말은 하지 마.\n\n"
    "제목: {title}\n내용: {content}"
)

# 데보션의 NEWS(일일 다이제스트)/DEVOTEE(자동 생성 트렌드) 탭은 rss.do
# 피드에 아예 포함되지 않는다(BLOG 탭 개인 작성 글만 내려옴). 별도 API도
# 없어 목록 페이지 HTML을 직접 긁어온다.
DEVOCEAN_TAB_URL = "https://devocean.sk.com/blog/index.do?p={tab}"
DEVOCEAN_DETAIL_URL = "https://devocean.sk.com/blog/techBoardDetail.do?ID={id}&boardType=techBlog"

DEVOCEAN_LIST_RE = re.compile(r'<ul class="sec-area-list01[^>]*>(.*?)</ul>', re.DOTALL)
DEVOCEAN_ITEM_RE = re.compile(r"<li>(.*?)</li>", re.DOTALL)
DEVOCEAN_ID_RE = re.compile(r"goDetail\(this,'(\d+)'")
DEVOCEAN_TITLE_RE = re.compile(r'<h3 class="title pc_view"[^>]*>(.*?)</h3>', re.DOTALL)
DEVOCEAN_DESC_RE = re.compile(r'<p class="desc"[^>]*>(.*?)</p>', re.DOTALL)
DEVOCEAN_DATE_RE = re.compile(r'<span class="date">([\d.]+)</span>')

# (탭 파라미터, 표시 소스명, region). BLOG 탭은 SOURCES의 rss.do로 이미
# 커버되므로 제외. 뉴스 다이제스트는 성격이 달라 국내/해외와 분리된
# "news" region으로 묶어 메시지 맨 아래에 배치한다.
DEVOCEAN_SCRAPED_TABS = [
    ("DEVOTEE", "데보션 트렌드", "domestic"),
    ("NEWS", "데보션 뉴스", "news"),
]


# 피드 엔트리의 발행/수정 시각을 UTC datetime으로 변환한다.
# published_parsed가 없는 피드(예: 네이버 D2)는 updated_parsed로 대체한다.
# 데보션처럼 pubDate에 타임존이 빠져 feedparser가 아예 못 읽는 경우, 원본
# 문자열을 KST 로컬시간으로 간주해 직접 파싱한다(한국 블로그라 KST가 맞음).
def _entry_datetime_utc(entry):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return datetime(*parsed[:6], tzinfo=timezone.utc)

    raw = entry.get("published") or entry.get("updated")
    if not raw:
        return None
    try:
        naive = datetime.strptime(raw.strip(), "%a, %d %b %Y %H:%M:%S")
    except ValueError:
        return None
    return naive.replace(tzinfo=KST).astimezone(timezone.utc)


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


# 텍스트 조각의 태그/공백을 정리해 평문 한 줄로 만든다.
def _clean_html_text(raw):
    return html.unescape(" ".join(raw.split()))


# 데보션 NEWS/DEVOTEE 탭의 글 목록을 스크래핑한다. 이 탭들은 RSS로 안
# 내려오는 유일한 통로가 목록 페이지 HTML이라, 정규식으로 직접 파싱한다.
# 날짜는 "26.07.29"처럼 일 단위로만 나오고 시각이 없어 다른 소스처럼
# lookback 시간으로는 비교할 수 없다. 평일 아침에 그 전날 글을 다루는 걸
# 전제로, KST 기준 어제 날짜인 글만 포함한다(당일 글은 아직 갱신 중일 수
# 있어 하루 지연시켜 안정적으로 잡는다).
def _fetch_devocean_tab_posts(tab, source_label, region, target_date_kst):
    res = requests.get(DEVOCEAN_TAB_URL.format(tab=tab), timeout=30)
    res.raise_for_status()

    list_match = DEVOCEAN_LIST_RE.search(res.text)
    if not list_match:
        return []

    posts = []
    for item in DEVOCEAN_ITEM_RE.findall(list_match.group(1)):
        id_match = DEVOCEAN_ID_RE.search(item)
        title_match = DEVOCEAN_TITLE_RE.search(item)
        date_match = DEVOCEAN_DATE_RE.search(item)
        if not (id_match and title_match and date_match):
            continue

        try:
            date = datetime.strptime(date_match.group(1), "%y.%m.%d").date()
        except ValueError:
            continue
        if date != target_date_kst:
            continue

        desc_match = DEVOCEAN_DESC_RE.search(item)
        posts.append({
            "source": source_label,
            "region": region,
            "title": _clean_html_text(title_match.group(1)),
            "link": DEVOCEAN_DETAIL_URL.format(id=id_match.group(1)),
            "text": _clean_html_text(desc_match.group(1)) if desc_match else "",
        })

    return posts


# 등록된 모든 소스에서 최근(평일 실행 간격 기준) 올라온 글만 모아 반환한다.
# 국내 글 -> 해외 글 -> 뉴스 다이제스트 순으로 배치한다.
def fetch_recent_posts():
    now_kst = datetime.now(KST)
    window_start = datetime.now(timezone.utc) - timedelta(hours=_lookback_hours(now_kst))
    domestic_posts = []
    international_posts = []
    news_posts = []
    region_targets = {
        "domestic": domestic_posts,
        "international": international_posts,
        "news": news_posts,
    }

    for source in SOURCES:
        feed = feedparser.parse(source["feed"])
        if feed.bozo and not feed.entries:
            print(f"[warn] 피드 파싱 실패 ({source['name']}): {feed.bozo_exception}")
            continue

        target = region_targets[source["region"]]
        for entry in feed.entries:
            entry_dt = _entry_datetime_utc(entry)
            if entry_dt is None or entry_dt < window_start:
                continue
            title = entry.get("title", "(제목 없음)")
            target.append({
                "source": source["name"],
                "region": source["region"],
                "title": title,
                "link": entry.get("link", ""),
                "text": _extract_text(entry),
            })

    yesterday_kst = (now_kst - timedelta(days=1)).date()
    for tab, source_label, region in DEVOCEAN_SCRAPED_TABS:
        try:
            region_targets[region].extend(
                _fetch_devocean_tab_posts(tab, source_label, region, yesterday_kst)
            )
        except Exception as e:
            print(f"[warn] {source_label} 목록 수집 실패: {e}")

    return (domestic_posts + international_posts + news_posts)[:MAX_POSTS]


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


# 요약문을 한 줄로 다듬는다. 모델이 개행을 넣어 보내는 경우가 있어 공백으로 합친다.
def format_summary(text):
    if not text:
        return ""
    return " ".join(text.split())


# 어떤 블로그를 모니터링 중인지 안내하는 context 블록.
# 글이 있는 날/없는 날 모두 붙여서, 채널에 새로 들어온 사람도 이 봇이
# 어떤 소스를 커버하는지 바로 알 수 있게 한다.
def _sources_context_block():
    links = [f"<{s['url']}|{s['name']}>" for s in SOURCES]
    links += [
        f"<{DEVOCEAN_TAB_URL.format(tab=tab)}|{label}>"
        for tab, label, _region in DEVOCEAN_SCRAPED_TABS
    ]
    return {
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": "모니터링 중: " + " · ".join(links),
        }],
    }


# 수집한 글 목록을 Slack Block Kit 형식으로 변환한다.
# 오늘 올라온 글이 하나도 없으면 안내 문구만 담은 블록을 반환한다.
def build_blocks(posts):
    blocks = [{
        "type": "header",
        "text": {"type": "plain_text", "text": "오늘의 개발 블로그 소식"},
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
    # 헤더/요약과 국내 섹션 사이 여백. Block Kit엔 빈 블록이 없어 공백 한 칸으로 대신한다.
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": " "}})

    domestic = [p for p in posts if p["region"] == "domestic"]
    international = [p for p in posts if p["region"] == "international"]
    news = [p for p in posts if p["region"] == "news"]

    for label, group in (("🇰🇷 국내", domestic), ("🌍 해외", international), ("📰 데보션 뉴스", news)):
        if not group:
            continue
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{label} · {len(group)}편*"},
        })
        for post in group:
            header = f"*[{post['source']}] <{post['link']}|{post['title']}>*"
            if post["region"] == "news":
                text = header
            else:
                summary = format_summary(summarize(post["title"], post["text"]))
                text = f"{header}\n\n{summary}" if summary else header
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": text},
            })
            blocks.append({"type": "divider"})

    blocks.append(_sources_context_block())
    return blocks
