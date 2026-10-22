"""東横イン キャンセル待ち通知

指定ホテル・日付・部屋タイプの空室を一定間隔で確認し、
満室 → 空室 に変わった瞬間に ntfy でスマホへプッシュ通知する。
"""
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
    "00201": "東横INN富山駅新幹線口1",
    "00004": "東横INN富山駅新幹線口2",
}
CHECKIN = "2026-11-10"
NIGHTS = 1
PEOPLE = 1
ROOMS = 1
ROOM_NAME = "シングル"          # 部屋タイプ名にこの文字を含むものを見る
NO_SMOKING = True              # True=禁煙のみ / False=喫煙のみ / None=どちらでも
# 資格が必要で一般には予約できないプランは空室判定から除く
EXCLUDE_PLANS = ("学割", "受験生")
# =========================================================

SITE = "https://www.toyoko-inn.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
JST = timezone(timedelta(hours=9))
STATE_FILE = Path(__file__).resolve().parent / "state.json"
REQUEST_GAP_SEC = 3             # ホテル間のアクセス間隔（サイト負荷対策）
FAIL_ALERT_AFTER = 5            # 連続失敗がこの回数に達したら1回だけ警告通知


class FetchError(Exception):
    pass


class MaintenanceError(FetchError):
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
    """公式サイトのプラン選択ページに埋め込まれた空室データを取り出す"""
    req = urllib.request.Request(booking_url(code), headers={
        "User-Agent": UA, "Accept-Language": "ja,en;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=40) as res:
            final_url = res.geturl()
            html = res.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 503 or "maintenance" in (e.geturl() or ""):
            raise MaintenanceError("東横インがメンテナンス中") from e
        raise FetchError(f"HTTP {e.code}") from e
    except Exception as e:  # noqa: BLE001
        raise FetchError(f"通信エラー: {e}") from e
    if "maintenance" in final_url or "システムメンテナンス" in html:
        raise MaintenanceError("東横インがメンテナンス中")
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise FetchError("ページ構造が想定と違う（アクセス制限かサイト変更の可能性）")
    plan = json.loads(m.group(1)).get("props", {}).get("pageProps", {}).get("planResponse")
    if not plan or "roomTypeList" not in plan:
        raise FetchError("空室データが見つからない")
    return plan


def count_vacant(plan: dict) -> tuple[int, int | None]:
    """条件に合う部屋の空室数と最安値（一般料金）を返す"""
    best_vac, best_price = 0, None
    for rt in plan.get("roomTypeList", []):
        if ROOM_NAME not in (rt.get("roomTypeName") or ""):
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
