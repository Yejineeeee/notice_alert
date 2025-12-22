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
UA = "Mozilla/5.0 (compatible; mju-notice-watcher/1.2)"

# 게시판 태그(메일 제목 짧게)
BOARD_TAG = {
    "일반공지": "일반",
    "행사공지": "행사",
    "학사공지": "학사",
    "장학/학자금공지": "장학",
    "진로/취업/창업공지": "진로",
    "학생활동공지": "활동",
}

BOARDS = [
    {"name": "일반공지", "url": "https://www.mju.ac.kr/mjukr/255/subview.do?fnctId=bbs&fnctNo=141"},
    {"name": "행사공지", "url": "https://www.mju.ac.kr/mjukr/256/subview.do?fnctId=bbs&fnctNo=142"},
    {"name": "학사공지", "url": "https://www.mju.ac.kr/mjukr/257/subview.do?fnctId=bbs&fnctNo=143"},
    {"name": "장학/학자금공지", "url": "https://www.mju.ac.kr/mjukr/259/subview.do?fnctId=bbs&fnctNo=145"},
    {"name": "진로/취업/창업공지", "url": "https://www.mju.ac.kr/mjukr/260/subview.do?fnctId=bbs&fnctNo=146"},
    {"name": "학생활동공지", "url": "https://www.mju.ac.kr/mjukr/5364/subview.do?fnctId=bbs&fnctNo=853"},
]


def _get_env_str(key: str, default: str | None = None) -> str:
    """Read env var as string. If missing/blank, return default (if provided) else ''."""
    val = os.environ.get(key)
    if val is None:
        return (default or "")
    val = val.strip()
    if val == "":
        return (default or "")
    return val


def _require_env_str(key: str) -> str:
    """Read env var and require non-empty."""
    val = _get_env_str(key, default=None)
    if not val:
        raise RuntimeError(f"{key} is missing or empty. Set it in GitHub Secrets.")
    return val


def _get_env_int(key: str, default: int) -> int:
    """Read env var as int. If missing/blank, return default. If invalid, raise helpful error."""
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError as e:
        raise RuntimeError(f"{key} must be an integer (e.g., {default}). Got: {raw!r}") from e


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
    for a in soup.select('a[href*="artclView.do"]'):
        href = a.get("href", "").strip()
        if not href:
            continue

        title = " ".join(a.get_text(" ", strip=True).split())
        if not title:
            continue

        full_url = href if href.startswith("http") else urljoin(list_url, href)

        m = re.search(r"/bbs/mjukr/\d+/(\d+)/artclView\.do", full_url)
        artcl_id = m.group(1) if m else full_url  # fallback

        date_text = None
        tr = a.find_parent("tr")
        if tr:
            txt = " ".join(tr.get_text(" ", strip=True).split())
            dm = re.search(r"\b(20\d{2}\.\d{2}\.\d{2})\b", txt)
            if dm:
                date_text = dm.group(1)

        items.append(
            {
                "id": str(artcl_id),
                "title": title,
                "date": date_text,
                "url": full_url,
            }
        )

    uniq = {it["id"]: it for it in items}
    items = list(uniq.values())

    def sort_key(x):
        return int(x["id"]) if x["id"].isdigit() else 0

    items.sort(key=sort_key, reverse=True)
    return items


def fetch_post_text(post_url: str) -> str:
    r = requests.get(post_url, headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    candidates = [
        soup.select_one(".view-con"),
        soup.select_one(".article"),
        soup.select_one(".bbs_view"),
        soup.select_one("#contents"),
    ]
    node = next((c for c in candidates if c), None)

    text = node.get_text(" ", strip=True) if node else soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def summarize_text(text: str, max_chars: int = 180) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_chars:
        return text

    cut = max_chars
    for pat in ["니다.", "다.", ".", "!", "?", "요."]:
        idx = text.rfind(pat, 0, max_chars)
        if idx >= 60:
            cut = idx + len(pat)
            break

    return text[:cut].rstrip() + "…"


def send_email(new_by_board):
    smtp_host = _get_env_str("SMTP_HOST", default="smtp.gmail.com")
    smtp_port = _get_env_int("SMTP_PORT", default=587)
    smtp_user = _require_env_str("SMTP_USER")
    smtp_pass = _require_env_str("SMTP_PASS")
    mail_to_raw = _require_env_str("MAIL_TO")
    mail_from = _get_env_str("MAIL_FROM", default=smtp_user)

    mail_to = [x.strip() for x in mail_to_raw.split(",") if x.strip()]
    if not mail_to:
        raise RuntimeError("MAIL_TO is empty after parsing. Provide at least one email address.")

    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    total = sum(len(v) for v in new_by_board.values())

    # Subject(메일 제목)은 서식 불가. 텍스트로만.
    active_tags = []
    for board_name, posts in new_by_board.items():
        if posts:
            active_tags.append(BOARD_TAG.get(board_name, board_name))
    tag_prefix = "".join([f"[{t}]" for t in active_tags])
    subject = f"{tag_prefix} [명지대 공지 알림] 새 게시물 {total}건 ({now_kst} KST)"

    # ---------- Plain text(대체 본문) ----------
    text_lines = [f"명지대학교 공지 새 글 알림 ({now_kst} KST)", ""]
    for board_name, posts in new_by_board.items():
        if not posts:
            continue
        text_lines.append(f"== {board_name} ({len(posts)}건) ==")
        for p in posts:
            d = f"({p['date']}) " if p.get("date") else ""
            text_lines.append(f"- {d}{p.get('title','')}")
            if p.get("summary"):
                text_lines.append(f"  요약: {p.get('summary','')}")
            text_lines.append(f"  {p.get('url','')}")
        text_lines.append("")

    # ---------- HTML(카드형 본문) ----------
    def esc(s: str) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    html = []
    html.append(f"""
    <div style="font-family: Arial, sans-serif; max-width: 760px;">
      <h1 style="margin:0 0 6px 0; font-size:22px; font-weight:700;">명지대학교 공지 새 글 알림</h1>
      <div style="color:#666; margin-bottom:14px;">{esc(now_kst)} (KST)</div>
    """)

    for board_name, posts in new_by_board.items():
        if not posts:
            continue

        html.append(f"""
        <h2 style="margin:18px 0 10px 0; font-size:16px;">{esc(board_name)} ({len(posts)}건)</h2>
        """)

        for p in posts:
            d = f"{p['date']} " if p.get("date") else ""
            title = p.get("title", "")
            url = p.get("url", "")
            summary = p.get("summary", "")

            html.append(f"""
            <div style="border:1px solid #e5e7eb; border-radius:12px; padding:12px 14px; margin:10px 0; background:#ffffff;">
              <div style="font-size:16px; font-weight:700; margin:0 0 6px 0;">
                {esc(d)}{esc(title)}
              </div>
              <div style="color:#333; line-height:1.5; margin:0 0 10px 0;">
                {esc(summary) if summary else "<span style='color:#888;'>요약을 가져오지 못했습니다.</span>"}
              </div>
              <a href="{esc(url)}"
                 style="display:inline-block; padding:8px 10px; border:1px solid #d1d5db; border-radius:10px; text-decoration:none; color:#111;">
                 게시글 바로가기
              </a>
            </div>
            """)

    html.append("</div>")

    msg = EmailMessage()
    msg["From"] = mail_from
    msg["To"] = ", ".join(mail_to)
    msg["Subject"] = subject

    # 텍스트 + HTML 멀티파트
    msg.set_content("\n".join(text_lines))
    msg.add_alternative("\n".join(html), subtype="html")

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
    send_backlog = _get_env_str("SEND_BACKLOG", default="false").lower() == "true"

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

        # 새 글에 대해서만 본문 요약 생성
        for p in new_posts:
            try:
                body_text = fetch_post_text(p["url"])
                p["summary"] = summarize_text(body_text, max_chars=180)
            except Exception:
                p["summary"] = ""

        # 오래된 것부터 출력
        new_by_board[name] = list(reversed(new_posts))

        # 상태 갱신(최대 500개)
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

