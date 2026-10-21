"""東横イン キャンセル待ち通知"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# ===== 監視条件（ここを書き換えれば条件を変えられる） =====
HOTELS = {
    "00171": "東横INN金沢兼六園香林坊",
    "00110": "東横INN金沢駅東口",
}
CHECKIN = "2026-11-12"
NIGHTS = 1
PEOPLE = 1
ROOMS = 1
ROOM_NAME = "シングル"          # 部屋タイプ名が完全一致するものだけ見る
NO_SMOKING = True              # True=禁煙のみ / False=喫煙のみ / None=どちらでも
EXCLUDE_PLANS = ("学割", "受験生")  # 資格が必要なプランは除外
# =========================================================

SITE = "https://www.toyoko-inn.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
JST = timezone(timedelta(hours=9))
STATE_FILE = Path(__file__).resolve().parent / "state.json"
REQUEST_GAP_SEC = 3
FAIL_ALERT_AFTER = 5


class FetchError(Exception):
    pass


def now_jst() -> datetime:
    return datetime.now(JST)


def checkout() -> str:
    return (date.fromisoformat(CHECKIN) + timedelta(days=NIGHTS)).isoformat()


def booking_url(code: str) -> str:
    q = {"hotel": code, "people": PEOPLE, "room": ROOMS,
         "smoking": "noSmoking" if NO_SMOKING else ("smoking" if NO_SMOKING is False else "all"),
         "start": CHECKIN, "end": checkout()}
    return f"{SITE}/search/result/room_plan/?" + urllib.parse.urlencode(q)


def fetch_plan(code: str) -> dict:
    req = urllib.request.Request(booking_url(code), headers={
        "User-Agent": UA, "Accept-Language": "ja,en;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=40) as res:
            html = res.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise FetchError(f"HTTP {e.code}") from e
    except Exception as e:  # noqa: BLE001
        raise FetchError(f"通信エラー: {e}") from e
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise FetchError("ページ構造が想定と違う（アクセス制限かサイト変更の可能性）")
    plan = json.loads(m.group(1)).get("props", {}).get("pageProps", {}).get("planResponse")
    if not plan or "roomTypeList" not in plan:
        raise FetchError("空室データが見つからない")
    return plan


def count_vacant(plan: dict) -> tuple[int, int | None]:
    best_vac, best_price = 0, None
    for rt in plan.get("roomTypeList", []):
        if rt.get("roomTypeName") != ROOM_NAME:
            continue
        smoking = bool(rt.get("specs", {}).get("isSmoking"))
        if NO_SMOKING is True and smoking:
            continue
        if NO_SMOKING is False and not smoking:
            continue
        for p in rt.get("plans", []):
            if any(x in (p.get("planName") or "") for x in EXCLUDE_PLANS):
                continue
            v = int((p.get("vacant") or {}).get("generalVacantRoom") or 0)
            if v <= 0:
                continue
            best_vac = max(best_vac, v)
            price = (p.get("price") or {}).get("generalPrice")
            if price and (best_price is None or price < best_price):
                best_price = price
    return best_vac, best_price


def push(title: str, message: str, url: str | None = None, priority: int = 5) -> None:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("  (NTFY_TOPIC 未設定のため通知スキップ)")
        return
    body = {"topic": topic, "title": title, "message": message,
            "priority": priority, "tags": ["hotel"]}
    if url:
        body["click"] = url
        body["actions"] = [{"action": "view", "label": "予約ページを開く", "url": url}]
    req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20):
            pass
    except Exception as e:  # noqa: BLE001
        print(f"  通知送信失敗: {e}")


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text("utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")


def check_all(state: dict) -> None:
    for i, (code, name) in enumerate(HOTELS.items()):
        if i:
            time.sleep(REQUEST_GAP_SEC)
        st = state.setdefault(code, {"vacant": 0, "fails": 0, "alerted": False})
        try:
            vac, price = count_vacant(fetch_plan(code))
        except FetchError as e:
            st["fails"] += 1
            print(f"{now_jst():%H:%M:%S} {name}: 取得失敗({st['fails']}) {e}")
            if st["fails"] >= FAIL_ALERT_AFTER and not st["alerted"]:
                push("⚠ 東横イン監視が止まっています",
                     f"{name} の空室を{st['fails']}回連続で取得できませんでした。\n{e}", priority=3)
                st["alerted"] = True
            continue
        st["fails"], st["alerted"] = 0, False
        prev = st.get("vacant", 0)
        print(f"{now_jst():%H:%M:%S} {name}: 空室 {prev} -> {vac}")
        if prev <= 0 < vac:
            price_txt = f" / {price:,}円〜" if price else ""
            push(f"🏨 空室あり！{name}",
                 f"{CHECKIN} から{NIGHTS}泊 {ROOM_NAME}{'禁煙' if NO_SMOKING else ''}\n"
                 f"残り{vac}室{price_txt}\nタップで予約ページへ",
                 booking_url(code))
        st["vacant"] = vac


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--minutes", type=float, default=55)
    ap.add_argument("--interval", type=int, default=120)
    args = ap.parse_args()

    if now_jst().date() > date.fromisoformat(CHECKIN):
        print("宿泊日を過ぎたので監視終了")
        return 0

    state = load_state()
    deadline = time.monotonic() + args.minutes * 60
    while True:
        check_all(state)
        save_state(state)
        if args.once or time.monotonic() + args.interval > deadline:
            break
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
