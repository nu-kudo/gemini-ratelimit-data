#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
収集専用スクリプト（クラウドルーチン連携用）。

gemini_ratelimit_watch.py の関数を再利用して、
  - レート制限ドキュメント / リリースノートの差分
  - Service-Tier の変化
  - Gemini 3.1 Flash-Lite の在否
を収集し、data/latest.json に出力する。

Chatwork への通知はここでは行わない（クラウドルーチン側が JSON を検証して通知する）。
差分はローカルのスナップショット（state/）と比較するため、
change_detected=true は「変化した当日に1回だけ」立つ。
"""

import os
import json
from datetime import datetime, timezone, timedelta

import gemini_ratelimit_watch as w

JST = timezone(timedelta(hours=9))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")


def process_page(t: dict) -> dict:
    """1ページ分を取得・比較し、結果 dict を返す。スナップショットも更新する。"""
    result = {
        "name": t["name"], "label": t["label"], "url": t["url"],
        "changed": False, "diff": "", "error": None,
    }
    try:
        page = w.fetch(t["url"])
    except Exception as e:  # noqa: BLE001
        result["error"] = f"取得失敗: {e}"
        return result

    new_text = w.extract_main_text(page)
    if len(new_text) < 200:
        result["error"] = f"本文が短すぎます ({len(new_text)} 文字)"
        return result

    sp = w.state_path(t["name"])
    if not os.path.exists(sp):
        with open(sp, "w", encoding="utf-8") as f:
            f.write(new_text)
        result["note"] = "初回スナップショット作成"
        return result

    with open(sp, encoding="utf-8") as f:
        old_text = f.read()

    # Flash-Lite 消失アラート（rate-limits のみ）
    if t["name"] == "rate-limits":
        result["watched_model_alerts"] = w.watched_model_alerts(old_text, new_text)

    if old_text == new_text:
        return result

    diff = (w.diff_keyword(old_text, new_text) if t["mode"] == "keyword"
            else w.diff_full(old_text, new_text))
    # 生テキストが変わったらスナップショットは必ず更新（変化を消費）
    with open(sp, "w", encoding="utf-8") as f:
        f.write(new_text)

    result["diff"] = diff
    # keyword モードは「関連行が有るときだけ」変化ありとする（軽微改稿の誤検知を防ぐ）
    result["changed"] = bool(diff) if t["mode"] == "keyword" else True
    return result


def process_service_tier(api_key: str) -> dict:
    res = {"current": None, "previous": None, "changed": False, "error": None}
    if not api_key or api_key.startswith("PUT_YOUR"):
        res["error"] = "API キー未設定"
        return res

    tier, status = w.fetch_service_tier(api_key)
    if not tier:
        res["error"] = f"取得失敗 (HTTP {status})"
        return res

    res["current"] = tier
    sp = os.path.join(w.STATE_DIR, "service-tier.txt")
    if not os.path.exists(sp):
        with open(sp, "w", encoding="utf-8") as f:
            f.write(tier)
        res["note"] = "初回記録"
        return res

    with open(sp, encoding="utf-8") as f:
        old = f.read().strip()
    res["previous"] = old
    if old != tier:
        with open(sp, "w", encoding="utf-8") as f:
            f.write(tier)
        res["changed"] = True
    return res


def main() -> int:
    os.makedirs(w.STATE_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    cfg = w.load_config()

    pages = [process_page(t) for t in w.TARGETS]
    tier = process_service_tier(cfg.get("gemini_key"))

    # 重点監視モデルの在否（最新スナップショットで判定）
    rl_path = w.state_path("rate-limits")
    rl_text = ""
    if os.path.exists(rl_path):
        with open(rl_path, encoding="utf-8") as f:
            rl_text = f.read().lower()
    watched = []
    for m in w.WATCHED_MODELS:
        watched.append({
            "name": m["name"],
            "label": m["label"],
            "baseline": m["baseline"],
            "present": m["name"].lower() in rl_text,
        })

    # 全体の change_detected 判定
    page_changed = any(p.get("changed") for p in pages)
    page_alerts = any(p.get("watched_model_alerts") for p in pages)
    change_detected = bool(page_changed or tier.get("changed") or page_alerts)

    out = {
        "schema": "gemini-ratelimit-watch/1",
        "generated_at": datetime.now(JST).isoformat(),
        "change_detected": change_detected,
        "pages": pages,
        "service_tier": tier,
        "watched_models": watched,
        "note": ("change_detected=true は変化のあった当日のみ。"
                 "クラウドルーチンはこの内容を検証し、Flash-Lite/レート制限に"
                 "影響する場合のみ Chatwork に [toall] 付きで通知する。"),
    }

    out_path = os.path.join(DATA_DIR, "latest.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    w.log(f"latest.json 出力 (change_detected={change_detected})")
    print(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
