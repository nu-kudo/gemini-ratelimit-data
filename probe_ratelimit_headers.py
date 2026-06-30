#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gemini API がレート制限値をレスポンスヘッダーで返すかを実測する調査用スクリプト。

使い方:
    GEMINI_API_KEY=xxxxx python3 probe_ratelimit_headers.py [モデルID]

  - API キーは環境変数 GEMINI_API_KEY から読む（ファイルには保存しない）。
  - countTokens（無料・課金されない軽量リクエスト）を投げ、返ってきた
    全レスポンスヘッダーを表示し、x-ratelimit-* / quota 系を強調する。
  - countTokens でヘッダーが取れなければ generateContent も試す。

このスクリプトは「監視本体」ではなく、ヘッダー方式が使えるかを確認するためのもの。
取得できると分かれば、watcher 側に数値監視を組み込む。
"""

import os
import sys
import json
import urllib.request
import urllib.error

API_KEY = os.environ.get("GEMINI_API_KEY")
MODEL = sys.argv[1] if len(sys.argv) > 1 else "gemini-flash-lite-latest"
BASE = "https://generativelanguage.googleapis.com/v1beta"


def call(endpoint: str, payload: dict):
    url = f"{BASE}/models/{MODEL}:{endpoint}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": API_KEY,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        # エラー時もヘッダーは確認したい
        return e.code, dict(e.headers), e.read().decode("utf-8", "ignore")


def show(title: str, status: int, headers: dict, body: str):
    print(f"\n===== {title} (HTTP {status}) =====")
    rate = {k: v for k, v in headers.items()
            if "ratelimit" in k.lower() or "quota" in k.lower()
            or "x-goog" in k.lower()}
    print("--- レート制限/quota 関連ヘッダー ---")
    if rate:
        for k, v in rate.items():
            print(f"  {k}: {v}")
    else:
        print("  （該当ヘッダーなし）")
    print("--- 全ヘッダー名 ---")
    print("  " + ", ".join(headers.keys()))
    if status >= 400:
        print("--- ボディ（先頭400字）---")
        print("  " + body[:400].replace("\n", " "))


def main() -> int:
    if not API_KEY:
        print("ERROR: 環境変数 GEMINI_API_KEY が未設定です。")
        print("使い方: GEMINI_API_KEY=xxxxx python3 probe_ratelimit_headers.py [モデルID]")
        return 2

    print(f"対象モデル: {MODEL}")

    s, h, b = call("countTokens", {
        "contents": [{"parts": [{"text": "ping"}]}]
    })
    show("countTokens", s, h, b)

    s, h, b = call("generateContent", {
        "contents": [{"parts": [{"text": "hi"}]}],
        "generationConfig": {"maxOutputTokens": 1},
    })
    show("generateContent", s, h, b)

    print("\n判定: 上の『レート制限/quota 関連ヘッダー』に "
          "x-ratelimit-limit / -remaining 等が出ていれば、数値の自動監視が可能です。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
