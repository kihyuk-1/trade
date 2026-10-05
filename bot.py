"""
미국 증시 뉴스 → 디스코드 알리미

Google 뉴스(국내)와 CNBC·로이터(해외) RSS를 주기적으로 확인해서, 새 기사를 디스코드 웹훅으로 보냅니다.
해외 기사는 제목을 한국어로 번역해서 바로 보냅니다.

준비:
    pip install feedparser requests

실행:
    python bot.py          # 계속 켜두고 1분마다 확인
    python bot.py --once   # 한 번만 확인하고 끝 (GitHub Actions 용)

※ 웹훅 URL은 비밀번호처럼 다루세요. 이 파일을 GitHub 등에 올릴 땐 URL을 지우고 올리세요.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import feedparser
import requests

# ───────────────────────── 설정 ─────────────────────────

# 디스코드 채널 설정 → 연동 → 웹후크 → "웹후크 URL 복사"한 값을 붙여넣으세요.
# GitHub Actions 에서는 저장소 Settings → Secrets 의 DISCORD_WEBHOOK_URL 로 넣습니다.
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

CHECK_INTERVAL_SEC = 60    # 몇 초마다 새 기사를 확인할지 (60 = 1분)
DIGEST_INTERVAL_MIN = 30   # 국내 일반 뉴스는 이 시간(분)마다 한 메시지로 모아서 보냄
MAX_DIGEST_ITEMS = 25      # 모아 보내기 한 번에 넣을 최대 기사 수 (넘치면 최신 기사 우선)
MAX_AGE_HOURS = 6          # 이보다 오래된 기사는 보내지 않음
HOT_MENTION = "@here"      # 🔥 중요 뉴스는 바로 보내면서 이 알림을 붙임 (끄려면 "")
BOT_NAME = "미국증시 알리미"


def google_news(query: str, lang: str = "ko") -> str:
    """Google 뉴스 검색 RSS 주소 만들기"""
    if lang == "ko":
        return f"https://news.google.com/rss/search?q={quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
    return f"https://news.google.com/rss/search?q={quote(query)}&hl=en-US&gl=US&ceid=US:en"


# 받고 싶은 뉴스 (이름: 주소). 검색어만 바꿔서 자유롭게 추가/삭제하세요.
FEEDS = {
    "뉴욕증시": google_news("뉴욕증시"),
    "미국증시": google_news("미국증시"),
    "나스닥": google_news("나스닥"),
    "연준": google_news("연준 금리"),
}

# 해외 뉴스 (이름: 주소). 제목을 한국어로 번역해서 모으지 않고 바로 보냄
OVERSEAS_FEEDS = {
    "CNBC": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    "로이터": google_news('(stocks OR markets OR "Wall Street" OR Fed) site:reuters.com', lang="en"),
}

# 이 단어가 제목에 있으면 🔥 중요 뉴스로 보고 바로 보냄 (비워두면 전부 모아 보내기)
HIGHLIGHT_KEYWORDS = [
    "FOMC", "CPI", "PCE", "금리 인상", "금리 인하", "파월", "고용보고서", "실업률", "급락", "폭락", "급등",
    "Powell", "rate cut", "rate hike", "jobs report", "plunge", "tumble", "sell-off", "selloff",
]

# 이 단어가 제목에 있으면 보내지 않음 (관련 없는 기사 걸러내기)
EXCLUDE_KEYWORDS = ["코인", "비트코인", "이더리움", "가상자산", "암호화폐", "리플", "상장 이전", "[포토]", "[부고]", "[인사]", "Bitcoin", "crypto"]

# 이 언론사 기사는 보내지 않음 (로그인/유료 구독해야 읽을 수 있는 곳 등). 이름 일부만 써도 됨
EXCLUDE_SOURCES = ["Investing.com", "네이버 프리미엄콘텐츠"]

# ───────────────────────── 내부 동작 ─────────────────────────

KST = timezone(timedelta(hours=9))
SEEN_FILE = Path(__file__).with_name("seen_news.json")
MAX_SEEN = 3000
HEADERS = {"User-Agent": "Mozilla/5.0 (stock-news-bot)"}


def load_seen() -> tuple[list, set]:
    """(이미 본 기사 목록, 이미 한 번 확인한 피드 이름들)"""
    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return [], set()
    if isinstance(data, list):  # 예전 형식
        return data, set()
    return data.get("items", []), set(data.get("feeds", []))


def save_seen(seen: list, known_feeds: set) -> None:
    data = {"items": seen[-MAX_SEEN:], "feeds": sorted(known_feeds)}
    SEEN_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def normalize_title(title: str) -> str:
    """'기사 제목 - 언론사' 에서 언론사를 떼고 비교용으로 정리 (피드 간 중복 제거용)"""
    title = re.sub(r"\s+-\s+[^-]+$", "", title)
    return re.sub(r"\W+", "", title).lower()


def fetch_feed(label: str, url: str, overseas: bool = False) -> list[dict]:
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)

    items = []
    for e in parsed.entries:
        title = e.get("title", "").strip()
        link = e.get("link", "")
        if not title or not link:
            continue

        published = None
        if e.get("published_parsed"):
            published = datetime(*e.published_parsed[:6], tzinfo=timezone.utc).astimezone(KST)

        source = e.get("source", {}).get("title", "") if isinstance(e.get("source"), dict) else ""
        if overseas and not source:
            source = label
        if source and title.endswith(f" - {source}"):
            title = title[: -len(f" - {source}")]

        items.append({
            "label": label,
            "title": title,
            "link": link,
            "source": source,
            "published": published,
            "key": e.get("id") or link,
            "title_key": normalize_title(title),
            "overseas": overseas,
        })
    return items


def send_to_discord(payload: dict) -> bool:
    for _ in range(3):
        resp = requests.post(WEBHOOK_URL, json=payload, timeout=15)
        if resp.status_code == 429:  # 너무 빨리 보내면 디스코드가 잠깐 기다리라고 함
            wait = float(resp.json().get("retry_after", 2))
            time.sleep(wait + 0.5)
            continue
        if resp.status_code >= 400:
            print(f"  [디스코드 오류] {resp.status_code}: {resp.text[:200]}")
            return False
        return True
    return False


def translate_ko(text: str) -> str:
    """영어 제목을 한국어로 (구글 번역 무료 주소 사용, 실패하면 원문 그대로)"""
    try:
        resp = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "en", "tl": "ko", "dt": "t", "q": text},
            headers=HEADERS, timeout=10,
        )
        resp.raise_for_status()
        return "".join(part[0] for part in resp.json()[0] if part[0]) or text
    except Exception:
        return text


def has_keyword(title: str, keywords: list[str]) -> bool:
    return any(k.lower() in title.lower() for k in keywords)


def post_hot_article(item: dict) -> bool:
    """중요 뉴스: 바로, 눈에 띄게 한 건씩"""
    time_str = item["published"].strftime("%m/%d %H:%M") if item["published"] else ""
    footer = " · ".join(x for x in [item["label"], time_str] if x)

    embed = {
        "title": "🔥 " + item["title"][:250],
        "url": item["link"],
        "color": 0xEF4444,
        "footer": {"text": footer},
    }
    if item["source"]:
        embed["description"] = f"📰 {item['source']}"

    payload = {"username": BOT_NAME, "embeds": [embed]}
    if HOT_MENTION:
        payload["content"] = HOT_MENTION
        payload["allowed_mentions"] = {"parse": ["everyone"]}
    return send_to_discord(payload)


def post_overseas_article(item: dict, hot: bool) -> bool:
    """해외 뉴스: 번역한 제목 + 원문 제목으로 바로 한 건씩"""
    time_str = item["published"].strftime("%m/%d %H:%M") if item["published"] else ""
    embed = {
        "title": ("🔥 " if hot else "🌎 ") + translate_ko(item["title"])[:250],
        "url": item["link"],
        "description": f"{item['title'][:300]}\n📰 {item['source']}",
        "color": 0xEF4444 if hot else 0x10B981,
        "footer": {"text": " · ".join(x for x in ["해외", time_str] if x)},
    }
    payload = {"username": BOT_NAME, "embeds": [embed]}
    if hot and HOT_MENTION:
        payload["content"] = HOT_MENTION
        payload["allowed_mentions"] = {"parse": ["everyone"]}
    return send_to_discord(payload)


def post_digest(items: list[dict]) -> None:
    """일반 뉴스: 여러 건을 한 메시지에 목록으로"""
    items = sorted(items, key=lambda it: it["published"] or datetime.now(KST))
    skipped = max(0, len(items) - MAX_DIGEST_ITEMS)
    items = items[skipped:]

    lines = []
    for it in items:
        # 제목 속 [ ] 는 디스코드 링크 문법을 깨뜨려서 전각 괄호로 바꿈
        title = it["title"][:120].replace("[", "［").replace("]", "］")
        meta = " · ".join(x for x in [it["source"], it["published"].strftime("%H:%M") if it["published"] else ""] if x)
        lines.append(f"• [{title}]({it['link']})" + (f"\n　{meta}" if meta else ""))
    if skipped:
        lines.append(f"\n…이전 기사 {skipped}건 생략")

    # 디스코드 embed 설명은 4096자 제한이라 길면 여러 메시지로 나눔
    chunks, cur = [], ""
    for line in lines:
        if cur and len(cur) + len(line) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)

    for i, chunk in enumerate(chunks):
        embed = {"description": chunk, "color": 0x3B82F6}
        if i == 0:
            embed["title"] = f"📰 미국 증시 뉴스 모음 ({len(items)}건)"
        if i == len(chunks) - 1:
            embed["footer"] = {"text": f"{datetime.now(KST):%m/%d %H:%M} 기준"}
        send_to_discord({"username": BOT_NAME, "embeds": [embed]})
        time.sleep(1.2)  # 디스코드 전송 속도 제한 대비


def check_once(seen: list, known_feeds: set) -> list[dict]:
    """새 기사 목록을 돌려줌 (보내는 건 main 에서). seen, known_feeds 는 직접 갱신됨"""
    seen_set = set(seen)
    new_items = []

    feeds = [(label, url, False) for label, url in FEEDS.items()]
    feeds += [(label, url, True) for label, url in OVERSEAS_FEEDS.items()]
    for label, url, overseas in feeds:
        try:
            items = fetch_feed(label, url, overseas)
        except Exception as ex:
            print(f"  [{label}] 가져오기 실패: {ex}")
            continue

        is_new_feed = label not in known_feeds
        skipped = 0
        for it in items:
            if it["key"] in seen_set or it["title_key"] in seen_set:
                continue
            seen_set.update([it["key"], it["title_key"]])
            seen.extend([it["key"], it["title_key"]])
            if is_new_feed:
                skipped += 1
            else:
                new_items.append(it)

        if is_new_feed:
            # 처음 보는 피드는 기존 기사를 한꺼번에 쏟아내지 않도록 '읽음' 처리만 함
            known_feeds.add(label)
            print(f"  [{label}] 처음 확인: 기존 기사 {skipped}개를 건너뜁니다. 이후 새 기사부터 보냅니다.")

    cutoff = datetime.now(KST) - timedelta(hours=MAX_AGE_HOURS)
    new_items = [
        it for it in new_items
        if (it["published"] is None or it["published"] >= cutoff)
        and not has_keyword(it["title"], EXCLUDE_KEYWORDS)
        and not has_keyword(it["source"], EXCLUDE_SOURCES)
    ]
    return new_items


def dispatch(new_items: list[dict], pending: list[dict]) -> None:
    """해외·중요 뉴스는 바로 보내고, 나머지는 pending 에 모음"""
    new_items.sort(key=lambda it: it["published"] or datetime.now(KST))
    for it in new_items:
        hot = has_keyword(it["title"], HIGHLIGHT_KEYWORDS)
        if it["overseas"]:
            if post_overseas_article(it, hot):
                print(f"  🌎 바로 보냄: {it['title'][:60]}")
            time.sleep(1.2)
        elif hot:
            if post_hot_article(it):
                print(f"  🔥 바로 보냄: {it['title'][:60]}")
            time.sleep(1.2)
        else:
            pending.append(it)


def run_once() -> None:
    """한 번 확인하고 끝. 국내 일반 뉴스는 이번에 찾은 것만 모아서 바로 보냄"""
    seen, known_feeds = load_seen()
    new_items = check_once(seen, known_feeds)
    save_seen(seen, known_feeds)

    pending: list[dict] = []
    dispatch(new_items, pending)
    if pending:
        post_digest(pending)
        print(f"  모아서 보냄: {len(pending)}건")
    print(f"완료: 새 기사 {len(new_items)}건")


def main() -> None:
    if not WEBHOOK_URL.startswith(("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")):
        print("DISCORD_WEBHOOK_URL 이 설정되지 않았어요. (GitHub: Settings → Secrets and variables → Actions)")
        sys.exit(1)

    if "--once" in sys.argv:
        run_once()
        return

    seen, known_feeds = load_seen()

    if send_to_discord({"username": BOT_NAME, "content": "✅ 미국 증시 뉴스 알리미가 시작됐어요."}):
        print("디스코드 연결 확인 완료")
    else:
        print("디스코드로 메시지를 보내지 못했어요. 웹훅 URL을 확인해 주세요.")
        sys.exit(1)

    print(f"{CHECK_INTERVAL_SEC}초마다 새 기사를 확인하고, 국내 일반 뉴스는 {DIGEST_INTERVAL_MIN}분마다 모아서, 해외 뉴스는 바로 보냅니다. (종료: Ctrl + C)")
    pending: list[dict] = []
    last_digest = time.monotonic()
    try:
        while True:
            now = datetime.now(KST).strftime("%H:%M:%S")
            print(f"[{now}] 확인 중... (대기 중인 기사 {len(pending)}건)")
            try:
                new_items = check_once(seen, known_feeds)
                save_seen(seen, known_feeds)

                dispatch(new_items, pending)

                if time.monotonic() - last_digest >= DIGEST_INTERVAL_MIN * 60:
                    if pending:
                        post_digest(pending)
                        print(f"  모아서 보냄: {len(pending)}건")
                        pending = []
                    last_digest = time.monotonic()
            except Exception as ex:
                print(f"  예상치 못한 오류 (계속 실행): {ex}")
            time.sleep(CHECK_INTERVAL_SEC)
    except KeyboardInterrupt:
        # 끄기 전에 모아둔 기사는 보내고 종료
        if pending:
            print(f"\n모아둔 기사 {len(pending)}건을 보내고 종료합니다.")
            post_digest(pending)
        raise


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n종료합니다.")