"""Ricco 앱 APK 다운로드 수 감시.

사용법
  python watch.py update  --config apps.json --state stats.json
      GitHub Releases API 로 다운로드 수를 읽어 state 를 갱신한다.
      증가분은 state 의 pending 에 쌓인다. 알릴 것이 있으면 GITHUB_OUTPUT 에 notify=true.
      API 오류가 하나라도 나면 state 를 건드리지 않고 실패(exit 1)한다.
  python watch.py notify  --config apps.json --state stats.json [--test]
      pending 을 이메일로 보내고, 성공하면 pending 을 0 으로 비운다.
      실패하면 state 를 그대로 두고 실패(exit 1) → 다음 실행 때 다시 보낸다.
      --test: 현재 누적 수로 테스트 메일만 보내고 state 는 바꾸지 않는다.

상태 파일(stats.json) 구조
  apps.<이름>.assets  : APK 파일(asset id)별 마지막으로 본 다운로드 수. 삭제된 릴리즈도 기억해 둔다.
  apps.<이름>.total   : 누적 다운로드 (줄어들지 않는다)
  apps.<이름>.pending : 아직 메일로 알리지 못한 증가분
"""

import argparse
import html
import json
import os
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

KST = timezone(timedelta(hours=9), "KST")  # 한국은 서머타임이 없어 UTC+9 고정
API = "https://api.github.com"


def now_kst():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M")


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def api_get(url):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ricco-download-watch",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last = e
            if e.code < 500 and e.code not in (403, 429):
                break  # 404 등은 다시 해도 같다
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"GitHub API 오류: {url} ({last})")


def fetch_apk_counts(repo):
    """저장소의 모든 릴리즈에 있는 .apk 파일별 다운로드 수 {asset_id: count}"""
    counts = {}
    page = 1
    while True:
        releases = api_get(f"{API}/repos/{repo}/releases?per_page=100&page={page}")
        if not isinstance(releases, list):
            raise RuntimeError(f"예상하지 못한 응답: {repo}")
        for rel in releases:
            for a in rel.get("assets", []):
                if a.get("name", "").lower().endswith(".apk"):
                    counts[str(a["id"])] = int(a.get("download_count", 0))
        if len(releases) < 100:
            return counts
        page += 1


def cmd_update(apps, state_path):
    state = load_json(state_path, {"version": 1, "apps": {}})
    fetched = {}
    for app in apps:  # 하나라도 실패하면 아무것도 저장하지 않는다
        fetched[app["name"]] = fetch_apk_counts(app["repo"])

    checked = now_kst()
    for app in apps:
        name = app["name"]
        cur = fetched[name]
        rec = state["apps"].get(name)
        if rec is None:
            # 처음 보는 앱: 현재 값을 기준값으로만 저장하고 알리지 않는다
            state["apps"][name] = {
                "repo": app["repo"],
                "assets": cur,
                "total": sum(cur.values()),
                "pending": 0,
                "baseline_at": checked,
            }
            print(f"{name}: 기준값 저장 {sum(cur.values())}회")
            continue
        prev = rec["assets"]
        delta = 0
        for aid, c in cur.items():
            p = prev.get(aid)
            # 새 APK 는 0 에서 시작하므로 전부 새 다운로드. 줄어든 경우(통계 초기화)는 0 으로 본다
            delta += c if p is None else max(0, c - p)
        merged = dict(prev)  # 지워진 릴리즈도 기억해 두어, 다시 보여도 새 다운로드로 오인하지 않는다
        merged.update(cur)
        rec.update(repo=app["repo"], assets=merged)
        rec["total"] += delta
        rec["pending"] += delta
        print(f"{name}: +{delta}회 (누적 {rec['total']}회, 미발송 {rec['pending']}회)")

    state["checked_at"] = checked
    save_json(state_path, state)
    notify = any(state["apps"].get(a["name"], {}).get("pending", 0) > 0 for a in apps)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"notify={'true' if notify else 'false'}\n")
    print("알림 필요" if notify else "알림 없음")


def build_mail(apps, state, test=False):
    checked = now_kst()
    rows_txt, rows_html, grand = [], [], 0
    for app in apps:
        rec = state["apps"].get(app["name"])
        if rec is None:
            continue
        inc, total = (0 if test else rec["pending"]), rec["total"]
        grand += total
        page = app["page"]
        rows_txt.append(f"{app['name']}\n  - 신규 다운로드: +{inc}회\n  - 누적 다운로드: {total}회\n  - 소개 페이지: {page}\n")
        hi = inc > 0
        box = ("background:#fff4e5;border:2px solid #ff8a3d;" if hi else "background:#f6f7f9;border:1px solid #e1e4e8;")
        badge = (f'<span style="color:#d9480f;font-weight:bold;">+{inc}회 🔥</span>' if hi
                 else f'<span style="color:#8a8f98;">+{inc}회</span>')
        rows_html.append(
            f'<div style="{box}border-radius:10px;padding:12px 16px;margin:10px 0;">'
            f'<div style="font-size:17px;font-weight:bold;margin-bottom:6px;">{html.escape(app["name"])}</div>'
            f'<div>신규 다운로드: {badge}</div>'
            f'<div>누적 다운로드: <b>{total}회</b></div>'
            f'<div>소개 페이지: <a href="{html.escape(page)}">{html.escape(page)}</a></div></div>')
    title = "[Ricco Apps] APK 다운로드 알림" + (" (테스트)" if test else "")
    head = "📦 Ricco 앱 다운로드 현황" + (" — 테스트 메일입니다" if test else "")
    text = head + "\n\n" + "\n".join(rows_txt) + f"\n전체 누적 다운로드: {grand}회\n확인 시각: {checked} (한국 시간)\n"
    body = (f'<div style="font-family:Apple SD Gothic Neo,Malgun Gothic,sans-serif;font-size:15px;line-height:1.6;max-width:560px;">'
            f'<h2 style="margin:0 0 8px;">{html.escape(head)}</h2>' + "".join(rows_html) +
            f'<p style="margin-top:14px;"><b>전체 누적 다운로드: {grand}회</b><br>확인 시각: {checked} (한국 시간)</p></div>')
    return title, text, body


def send_mail(title, text, body):
    user = os.environ.get("SMTP_USERNAME", "").strip()
    pw = os.environ.get("SMTP_PASSWORD", "").strip()
    to = [x.strip() for x in os.environ.get("MAIL_TO", "").split(",") if x.strip()]
    if not (user and pw and to):
        raise RuntimeError("SMTP_USERNAME / SMTP_PASSWORD / MAIL_TO 시크릿이 설정되지 않았습니다")
    host = os.environ.get("SMTP_SERVER", "").strip() or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT", "").strip() or "465")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = title
    sender = os.environ.get("MAIL_FROM", "").strip() or user
    if "@" not in sender and host.endswith("naver.com"):
        sender += "@naver.com"  # 네이버는 아이디만으로 로그인하지만 보내는 주소는 아이디@naver.com 이어야 한다
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(body, "html", "utf-8"))
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=60) as s:
            s.login(user, pw)
            s.sendmail(msg["From"], to, msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=60) as s:
            s.starttls(context=ctx)
            s.login(user, pw)
            s.sendmail(msg["From"], to, msg.as_string())


def cmd_notify(apps, state_path, test):
    state = load_json(state_path, None)
    if not state or not state.get("apps"):
        raise RuntimeError("상태 파일이 없습니다. 먼저 update 를 실행하세요")
    if not test and not any(r.get("pending", 0) > 0 for r in state["apps"].values()):
        print("보낼 알림 없음")
        return
    title, text, body = build_mail(apps, state, test)
    send_mail(title, text, body)
    print("메일 발송 완료")
    if not test:
        for rec in state["apps"].values():
            rec["pending"] = 0
        state["notified_at"] = now_kst()
        save_json(state_path, state)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["update", "notify"])
    p.add_argument("--config", required=True)
    p.add_argument("--state", required=True)
    p.add_argument("--test", action="store_true")
    a = p.parse_args()
    apps = load_json(a.config, [])
    try:
        if a.command == "update":
            cmd_update(apps, a.state)
        else:
            cmd_notify(apps, a.state, a.test)
    except Exception as e:  # noqa: BLE001 - Actions 로그에 원인만 남기고 실패 처리
        print(f"::error::{e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
