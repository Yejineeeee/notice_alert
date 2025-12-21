import os
import json
import re
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
import smtplib
import ssl
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

KST = timezone(timedelta(hours=9))
STATE_PATH = "state.json"
UA = "Mozilla/5.0 (compatible; mju-notice-watcher/1.1)"

# 게시판 태그(메일 제목 짧게)
BOARD_TAG = {
    "일반공지": "일반",
    "행사공지": "행사",
    "학사공지": "학사",
    "장학/학자금공지": "장학",
    "진로/취업/창업공지": "진로",
    "학생활동공지": "활동",
}

# 게시판 목록 (필요 시 fnctNo만 수정)
# ※ 아래 URL은 “게시판 목록 페이지” 역할만 하면 됩니다.
#   사용자가 준 subview 페이지를 그대로 써도 되고, /bbs/.../artclList.do 를 써도 됩니다.
BOARDS = [
    {"name": "일반공지", "url": "https://www.mju.ac.kr/mjukr/255/subview.do?fnctId=bbs&fnctNo=141"},
    {"name": "행사공지", "url": "https://www.mju.ac.kr/mjukr/256/subview.do?fnctId=bbs&fnctNo=142"},
    {"name": "학사공지", "url": "https://www.mju.ac.kr/mjukr/257/subview.do?fnctId=bbs&fnctNo=143"},
    {"name": "장학/학자금공지", "url": "https://www.mju.ac.kr/mjukr/259/subview.do?fnctId=bbs&fnctNo=145"},
    {"name": "진로/취업/창업공지", "url": "https://www.mju.ac.kr/mjukr/260/subview.do?fnctId=bbs&fnctNo=146"},
    {"name": "학생활동공지", "url": "https://www.mju.ac.kr/mjukr/5364/subview.do?fnctId=bbs&fnctNo=853"},
]

def load_state():
    if not os.path.exists(STATE_PATH):
        return {}
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def extract_articles(list_url: str):
    r = requests.get(list_url, headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    items = []
    # 게시글 링크는 보통 .../artclView.do 포함
    for a in soup.select('a[href*="artclView.do"]'):
        href = a.get("href", "").strip()
        if not href:
            continue

        title = " ".join(a.get_text(" ", strip=True).split())
        if not title:
            continue

        full_url = href if href.startswith("http") else urljoin(list_url, href)

        # 게시글 ID 추출(가능하면 숫자 ID로 중복 판정 안정화)
        m = re.search(r"/bbs/mjukr/\d+/(\d+)/artclView\.do", full_url)
        artcl_id = m.group(1) if m else full_url  # 최후 fallback은 URL 자체

        # 같은 행에서 날짜(YYYY.MM.DD) 추출
        date_text = None
        tr = a.find_parent("tr")
        if tr:
            txt = " ".join(tr.get_text(" ", strip=True).split())
            dm = re.search(r"\b(20\d{2}\.\d{2}\.\d{2})\b", txt)
            if dm:
                date_text = dm.group(1)

        items.append({
            "id": str(artcl_id),
            "title": title,
            "date": date_text,
            "url": full_url,
        })

    # ID 기준 중복 제거 후 정렬(숫자 ID면 내림차순)
    uniq = {}
    for it in items:
        uniq[it["id"]] = it
    items = list(uniq.values())

    def sort_key(x):
        return int(x["id"]) if x["id"].isdigit() else 0

    items.sort(key=sort_key, reverse=True)
    return items

def send_email(new_by_board):
    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASS"]

    mail_to = [x.strip() for x in os.environ["MAIL_TO"].split(",") if x.strip()]
    mail_from = os.environ.get("MAIL_FROM", smtp_user)

    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    total = sum(len(v) for v in new_by_board.values())

    # 제목 태그: 새 글이 있는 게시판만
    active_tags = []
    for board_name, posts in new_by_board.items():
        if posts:
            active_tags.append(BOARD_TAG.get(board_name, board_name))
    tag_prefix = "".join([f"[{t}]" for t in active_tags])  # 예: [학사][장학]

    subject = f"{tag_prefix} [명지대 공지 알림] 새 게시물 {total}건 ({now_kst} KST)"

    lines = []
    lines.append(f"명지대학교 공지 새 글 알림 ({now_kst} KST)")
    lines.append("")

    for board_name, posts in new_by_board.items():
        if not posts:
            continue
        lines.append(f"== {board_name} ({len(posts)}건) ==")
        for p in posts:
            d = f"({p['date']}) " if p.get("date") else ""
            lines.append(f"- [{board_name}] {d}{p['title']}")
            lines.append(f"  {p['url']}")
        lines.append("")

    msg = EmailMessage()
    msg["From"] = mail_from
    msg["To"] = ", ".join(mail_to)
    msg["Subject"] = subject
    msg.set_content("\n".join(lines))

    context = ssl.create_default_context()
    if smtp_port == 465:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context, timeout=30) as s:
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)
    else:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as s:
            s.ehlo()
            s.starttls(context=context)
            s.ehlo()
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)

def main():
    state = load_state()
    send_backlog = os.environ.get("SEND_BACKLOG", "false").lower() == "true"

    new_by_board = {}

    for b in BOARDS:
        name, url = b["name"], b["url"]
        articles = extract_articles(url)
        ids = [a["id"] for a in articles]

        # 최초 실행 시: 과거 글 메일 폭탄 방지(기준만 저장)
        if name not in state and not send_backlog:
            state[name] = ids[:300]
            new_by_board[name] = []
            continue

        seen = set(state.get(name, []))
        new_posts = [a for a in articles if a["id"] not in seen]

        # 안전장치: 한 번에 너무 많이 쌓이면 최신 50개만 발송
        new_posts = new_posts[:50]

        # 오래된 것부터 본문에 출력
        new_by_board[name] = list(reversed(new_posts))

        # 상태 갱신: 최신 목록 + 과거 seen (최대 500개)
        merged = list(dict.fromkeys(ids + list(seen)))
        state[name] = merged[:500]

    save_state(state)

    total_new = sum(len(v) for v in new_by_board.values())
    if total_new > 0:
        send_email(new_by_board)
        print(f"Sent email: {total_new} new posts")
    else:
        print("No new posts")

if __name__ == "__main__":
    main()
