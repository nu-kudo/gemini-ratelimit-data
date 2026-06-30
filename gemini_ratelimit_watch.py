#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gemini API のレート制限に関する変更を監視し、変化があれば Chatwork に通知する。

監視対象:
  1. レート制限ドキュメント (rate-limits)  -> 本文に変化があれば全差分を通知
  2. リリースノート (changelog)             -> レート制限関連の追記のみ通知

前回取得した本文スナップショットを state/ 配下に保存し、毎回それと比較する。
初回実行時はスナップショットを作成するだけで通知はしない。
"""

import os
import re
import sys
import json
import time
import html
import difflib
import urllib.parse
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE_DIR, "state")
LOG_FILE = os.path.join(BASE_DIR, "watch.log")

JST = timezone(timedelta(hours=9))

# Service-Tier 監視で叩くモデル（軽量リクエストでヘッダーを読むだけ）
GEMINI_MODEL_FOR_TIER = "gemini-flash-lite-latest"

# レート制限「関連」と判定するキーワード（changelog のフィルタに使用）
RATE_KEYWORDS = [
    "rate limit", "rate-limit", "ratelimit",
    "quota", "rpm", "tpm", "rpd", "requests per",
    "tokens per", "tier 1", "tier 2", "tier 3",
    "usage tier", "free tier", "throughput",
]

# モデルの提供終了・縮小を示すキーワード
DEPRECATION_KEYWORDS = [
    "deprecat", "retire", "sunset", "discontinu", "shut down",
    "shutting down", "no longer", "end of life", " eol", "removed",
    "decommission", "turn down", "turndown",
]

# 重点監視するモデル。公開ページには数値が載らないため baseline は参考表示用。
WATCHED_MODELS = [
    {
        "name": "Gemini 3.1 Flash-Lite",   # ページ上の表記（ハイフン）
        "label": "Gemini 3.1 Flash-Lite（テキスト出力）",
        "baseline": "RPM 4K / TPM 4M / RPD 150K（利用者申告値・要 AI Studio 確認）",
        # 行内検出用エイリアス（小文字）
        "aliases": ["gemini 3.1 flash-lite", "flash-lite"],
    },
]

TARGETS = [
    {
        "name": "rate-limits",
        "label": "レート制限ドキュメント",
        "url": "https://ai.google.dev/gemini-api/docs/rate-limits",
        "mode": "full",          # 本文の差分をすべて通知
    },
    {
        "name": "changelog",
        "label": "リリースノート",
        "url": "https://ai.google.dev/gemini-api/docs/changelog",
        "mode": "keyword",       # 追加行のうちレート制限関連のみ通知
    },
]


def log(msg: str) -> None:
    line = f"[{datetime.now(JST):%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_config() -> dict:
    """config.json または環境変数から Chatwork 認証情報を読む。"""
    cfg = {}
    cfg_path = os.path.join(BASE_DIR, "config.json")
    if os.path.exists(cfg_path):
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
    token = os.environ.get("CHATWORK_API_TOKEN") or cfg.get("chatwork_api_token")
    room_id = os.environ.get("CHATWORK_ROOM_ID") or cfg.get("chatwork_room_id")
    if not token or not room_id:
        log("ERROR: Chatwork の API トークン / ルームID が未設定です "
            "(config.json か環境変数 CHATWORK_API_TOKEN / CHATWORK_ROOM_ID)。")
        sys.exit(2)
    # Gemini API キーは任意（あれば Service-Tier 監視を有効化）
    gemini_key = os.environ.get("GEMINI_API_KEY") or cfg.get("gemini_api_key")
    return {"token": token, "room_id": str(room_id), "gemini_key": gemini_key}


def fetch(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; GeminiRateLimitWatcher/1.0)",
            "Accept-Language": "en",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="ignore")


def extract_main_text(page_html: str) -> str:
    """<main>本文</main> を取り出し、可読テキストの行リスト（改行区切り）に正規化する。"""
    m = re.search(r"<main\b[^>]*>(.*?)</main>", page_html, flags=re.S | re.I)
    body = m.group(1) if m else page_html

    body = re.sub(r"<script.*?</script>", " ", body, flags=re.S | re.I)
    body = re.sub(r"<style.*?</style>", " ", body, flags=re.S | re.I)
    # ブロック要素を改行に変換して行構造を保つ
    body = re.sub(r"</(p|div|li|tr|h[1-6]|section|article|table)>", "\n",
                  body, flags=re.I)
    body = re.sub(r"<br\s*/?>", "\n", body, flags=re.I)
    body = re.sub(r"<[^>]+>", " ", body)
    body = html.unescape(body)

    lines = []
    for ln in body.split("\n"):
        ln = re.sub(r"[ \t ]+", " ", ln).strip()
        if ln:
            lines.append(ln)
    return "\n".join(lines)


def state_path(name: str) -> str:
    return os.path.join(STATE_DIR, f"{name}.txt")


def diff_full(old: str, new: str) -> str:
    """全差分（追加/削除）を読みやすい形にまとめる。"""
    diff = difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        lineterm="", n=1,
    )
    out = []
    for ln in diff:
        if ln.startswith("+++") or ln.startswith("---") or ln.startswith("@@"):
            continue
        if ln.startswith("+"):
            out.append("（追加）" + ln[1:].strip())
        elif ln.startswith("-"):
            out.append("（削除）" + ln[1:].strip())
    return "\n".join(out).strip()


def diff_keyword(old: str, new: str) -> str:
    """追加行のうち、レート制限関連 / 重点モデル / 廃止系キーワードを含むものを抽出。

    重点モデル・廃止系に該当する行には ★重点 マークを付ける。
    """
    old_set = set(old.splitlines())
    added = [ln for ln in new.splitlines() if ln not in old_set]
    hits = []
    for ln in added:
        low = ln.lower()
        is_model = any(a in low for m in WATCHED_MODELS for a in m["aliases"])
        is_dep = any(k in low for k in DEPRECATION_KEYWORDS)
        is_rate = any(k in low for k in RATE_KEYWORDS)
        if is_model or is_dep or is_rate:
            mark = "★重点 " if (is_model or is_dep) else ""
            hits.append(f"（追加）{mark}{ln.strip()}")
    return "\n".join(hits).strip()


def watched_model_alerts(old_text: str, new_text: str) -> list:
    """重点監視モデルが一覧から消えた場合に高優先アラートを返す。"""
    alerts = []
    old_low, new_low = old_text.lower(), new_text.lower()
    for m in WATCHED_MODELS:
        key = m["name"].lower()
        if key in old_low and key not in new_low:
            alerts.append(
                f"⚠️ {m['label']} がレート制限ページのモデル一覧から消えました。"
                "提供終了・名称変更の可能性があります。至急 AI Studio で確認してください。"
            )
    return alerts


def fetch_service_tier(api_key: str) -> tuple:
    """generateContent を1回叩き X-Gemini-Service-Tier ヘッダーを返す。

    戻り値: (tier文字列 or None, HTTPステータス or None)
    """
    url = (f"https://generativelanguage.googleapis.com/v1beta/"
           f"models/{GEMINI_MODEL_FOR_TIER}:generateContent")
    payload = {
        "contents": [{"parts": [{"text": "ping"}]}],
        "generationConfig": {"maxOutputTokens": 1},
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.headers.get("X-Gemini-Service-Tier"), resp.status
    except urllib.error.HTTPError as e:
        # 429 等でもヘッダーは読める場合がある
        return e.headers.get("X-Gemini-Service-Tier"), e.code
    except Exception as e:  # noqa: BLE001
        log(f"WARN: Service-Tier 取得で例外: {e}")
        return None, None


def check_service_tier(api_key: str, priority_alerts: list) -> None:
    """サービスティアの変化を検知し、変われば重点アラートに積む。"""
    tier, status = fetch_service_tier(api_key)
    if not tier:
        log(f"WARN: Service-Tier を取得できませんでした (HTTP {status})")
        return

    sp = os.path.join(STATE_DIR, "service-tier.txt")
    if not os.path.exists(sp):
        with open(sp, "w", encoding="utf-8") as f:
            f.write(tier)
        log(f"Service-Tier 初回記録: {tier}（通知なし）")
        return

    with open(sp, encoding="utf-8") as f:
        old = f.read().strip()

    if old == tier:
        log(f"Service-Tier 変化なし: {tier}")
        return

    with open(sp, "w", encoding="utf-8") as f:
        f.write(tier)
    alert = (f"⚠️ Gemini API のサービスティアが変化しました:「{old}」→「{tier}」。"
             "レート制限（RPM/TPM/RPD）が変わった可能性があります。"
             "AI Studio で実数値を確認してください。")
    log("重点アラート: " + alert)
    priority_alerts.append(alert)


def post_chatwork(cfg: dict, body: str) -> None:
    url = f"https://api.chatwork.com/v2/rooms/{cfg['room_id']}/messages"
    data = urllib.parse.urlencode({"body": body}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "X-ChatWorkToken": cfg["token"],
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        log(f"Chatwork 送信完了 (HTTP {resp.status})")


def build_message(reports: list, priority_alerts: list) -> str:
    today = f"{datetime.now(JST):%Y-%m-%d}"
    inner = ["[title]Gemini API レート制限 変更検知 "
             f"({today})[/title]"]

    if priority_alerts:
        inner.append("【重点監視アラート】\n" + "\n".join(priority_alerts))

    for r in reports:
        inner.append(f"■ {r['label']}\n{r['url']}\n{r['diff']}")

    # 重点監視モデルの基準値を毎回末尾に明示（AI Studio での確認用）
    watch_note = "\n".join(
        f"・{m['label']}: {m['baseline']}" for m in WATCHED_MODELS
    )
    inner.append(
        "［重点監視モデルの基準］\n" + watch_note +
        "\n※ 数値の実値は公開ページに無く、AI Studio のダッシュボードで確認が必要です。"
    )

    # [toall] は info ブロックの外（本文先頭）に置く
    return "[toall]\n[info]" + "\n\n".join(inner) + "[/info]"


def main() -> int:
    os.makedirs(STATE_DIR, exist_ok=True)
    cfg = load_config()
    reports = []
    priority_alerts = []

    for t in TARGETS:
        try:
            page = fetch(t["url"])
        except Exception as e:  # noqa: BLE001
            log(f"WARN: 取得失敗 {t['name']}: {e}")
            continue

        new_text = extract_main_text(page)
        if len(new_text) < 200:
            log(f"WARN: 本文が短すぎます（取得失敗の可能性） {t['name']}: {len(new_text)} 文字")
            continue

        sp = state_path(t["name"])
        if not os.path.exists(sp):
            with open(sp, "w", encoding="utf-8") as f:
                f.write(new_text)
            log(f"初回スナップショット作成: {t['name']}（通知なし）")
            continue

        with open(sp, encoding="utf-8") as f:
            old_text = f.read()

        # 重点監視モデルの消失チェック（rate-limits ページで実施）
        if t["name"] == "rate-limits":
            for a in watched_model_alerts(old_text, new_text):
                log("重点アラート: " + a)
                priority_alerts.append(a)

        if old_text == new_text:
            log(f"変更なし: {t['name']}")
            continue

        if t["mode"] == "keyword":
            d = diff_keyword(old_text, new_text)
        else:
            d = diff_full(old_text, new_text)

        # スナップショットは常に最新へ更新（次回の基準）
        with open(sp, "w", encoding="utf-8") as f:
            f.write(new_text)

        if not d:
            log(f"変更あり（ただしレート制限非関連のため通知対象外）: {t['name']}")
            continue

        # Chatwork のメッセージが長くなりすぎないよう上限を設ける
        if len(d) > 2500:
            d = d[:2500] + "\n…（差分が長いため省略。詳細はページを確認）"

        log(f"変更検知: {t['name']}")
        reports.append({"label": t["label"], "url": t["url"], "diff": d})

    # Service-Tier 監視（Gemini API キーがある場合のみ）
    if cfg.get("gemini_key"):
        check_service_tier(cfg["gemini_key"], priority_alerts)
    else:
        log("Gemini API キー未設定のため Service-Tier 監視はスキップ")

    if reports or priority_alerts:
        post_chatwork(cfg, build_message(reports, priority_alerts))
    else:
        log("通知対象の変更はありませんでした。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
