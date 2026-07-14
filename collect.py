"""
ホルムズ海峡ステータス — 全自動コレクタ

GitHub Actions が30分ごとにこれを実行し、docs/status.json を書き換えて
コミットする。人間の承認は一切入らない。

だからこそ、嘘をつかない設計が要る:

  1. 「情報が無い」を「平常」の根拠にしない。
     情報源が2つ以上生きていることを確認できたときだけ判定する。
     取れなければ「営業中」ではなく「確認中」。

  2. 判定はルールベース。LLMは翻訳にしか使わない。
     再現性と説明可能性が命。なぜそう判定したかを reason に必ず書く。

  3. 発言は創作しない。公式発表の見出しをそのまま取り、機械翻訳し、
     原文と出典リンクを必ず併記する。

  4. 根拠の種類を隠さない。通航実績(AIS)は見ていない。
     当局が閉鎖を報告していないこと「だけ」が根拠だと画面に書く。

env (GitHub Secrets):
  DEEPL_KEY          … 任意。無ければ ANTHROPIC_API_KEY を使う
  ANTHROPIC_API_KEY  … 任意。両方無ければ原文のまま表示する
"""

from __future__ import annotations
import os, json, re, html, datetime as dt
from pathlib import Path

import httpx, feedparser

UTC = dt.timezone.utc
NOW = dt.datetime.now(UTC)
DOCS = Path(__file__).parent / "docs"
CACHE = DOCS / "translations.json"      # 翻訳キャッシュもリポジトリに残す
HISTORY = DOCS / "history.json"         # 日次ログ = 「閉鎖されなかった日数」の根拠

# ══════════════════════════════════════════════════════════
# 情報源
# ══════════════════════════════════════════════════════════
ADVISORY_FEEDS = {
    "UKMTO": "https://www.ukmto.org/rss/incidents",
    "MARAD": "https://www.maritime.dot.gov/rss/advisories.xml",
    "NGA":   "https://msi.nga.mil/api/publications/broadcast-warn?navArea=IX&output=rss",
}
STATEMENT_FEEDS = {
    "iran": [("IRNA", "https://en.irna.ir/rss"),
             ("Press TV", "https://www.presstv.ir/rss.xml")],
    "usa":  [("White House", "https://www.whitehouse.gov/news/feed/"),
             ("State Dept", "https://www.state.gov/rss-feed/press-releases/feed/")],
}

AREA_WORDS     = ("hormuz", "persian gulf", "arabian gulf", "gulf of oman", "bandar abbas")
INCIDENT_WORDS = ("attack", "seiz", "board", "hijack", "explos", "drone", "missile", "struck")
CLOSURE_WORDS  = ("clos", "shut", "block", "blockad", "halt traffic", "suspend traffic")


# ══════════════════════════════════════════════════════════
# 翻訳（自動翻訳であることを画面に必ず出す。原文も必ず残す）
# ══════════════════════════════════════════════════════════
_cache: dict[str, str] = json.loads(CACHE.read_text()) if CACHE.exists() else {}

def translate(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    if text in _cache:
        return _cache[text]
    ja = _deepl(text) or _claude(text) or text     # 失敗したら原文のまま（勝手な意訳より安全）
    _cache[text] = ja
    return ja


def _deepl(text):
    key = os.environ.get("DEEPL_KEY")
    if not key:
        return None
    host = "api-free" if key.endswith(":fx") else "api"
    try:
        r = httpx.post(f"https://{host}.deepl.com/v2/translate",
                       data={"auth_key": key, "text": text, "target_lang": "JA"}, timeout=15)
        r.raise_for_status()
        return r.json()["translations"][0]["text"]
    except Exception as e:
        print(f"  [warn] deepl: {e}")
        return None


def _claude(text):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        r = httpx.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 300,
                  "system": ("ニュース見出しを日本語に訳す。訳文だけを返す。"
                             "説明・前置き・引用符は不要。"
                             "意訳・要約・脚色・誇張をしない。断定の強さを変えない。"),
                  "messages": [{"role": "user", "content": text}]},
            timeout=25)
        r.raise_for_status()
        return r.json()["content"][0]["text"].strip()
    except Exception as e:
        print(f"  [warn] claude: {e}")
        return None


# ══════════════════════════════════════════════════════════
# 収集
# ══════════════════════════════════════════════════════════
def entries(url):
    try:
        f = feedparser.parse(url)
        return f.entries if f.entries else []
    except Exception as e:
        print(f"  [warn] feed dead: {url} ({e})")
        return []


def when(e):
    for k in ("published_parsed", "updated_parsed"):
        if e.get(k):
            return dt.datetime(*e[k][:6], tzinfo=UTC)
    return None


def clean(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def collect_advisories(days=90):
    since, out, alive = NOW - dt.timedelta(days=days), [], 0
    for src, url in ADVISORY_FEEDS.items():
        es = entries(url)
        if es:
            alive += 1
        for e in es:
            title = clean(e.get("title"))
            body  = clean(e.get("summary"))
            text  = (title + " " + body).lower()
            if not any(w in text for w in AREA_WORDS):
                continue
            t = when(e)
            if not t or t < since:
                continue
            out.append({
                "kind": "advisory",
                "source": src,
                "title": title,                 # 原文。改変しない
                "title_ja": translate(title),   # 自動翻訳
                "url": e.get("link"),
                "published": t.isoformat(),
                "is_incident": any(w in text for w in INCIDENT_WORDS),
                "is_closure":  any(w in text for w in CLOSURE_WORDS),
            })
    return sorted(out, key=lambda x: x["published"], reverse=True), alive


def collect_statements(days=120):
    """公式発言。ホルムズに言及したものだけ。創作しない。"""
    since, out = NOW - dt.timedelta(days=days), []
    for side, feeds in STATEMENT_FEEDS.items():
        for name, url in feeds:
            for e in entries(url):
                title = clean(e.get("title"))
                text  = (title + " " + clean(e.get("summary"))).lower()
                if "hormuz" not in text:
                    continue
                t = when(e)
                if not t or t < since:
                    continue
                out.append({
                    "kind": "statement",
                    "side": side,
                    "source": name,
                    "title": title,
                    "title_ja": translate(title),
                    "url": e.get("link"),
                    "published": t.isoformat(),
                    "is_closure": any(w in text for w in CLOSURE_WORDS),
                })
    return sorted(out, key=lambda x: x["published"], reverse=True)


def fetch_brent():
    try:
        r = httpx.get("https://query1.finance.yahoo.com/v8/finance/chart/BZ=F",
                      timeout=15, headers={"User-Agent": "hormuz-status/1.0 (+github)"})
        m = r.json()["chart"]["result"][0]["meta"]
        return {"price": round(m["regularMarketPrice"], 2),
                "change_pct": round((m["regularMarketPrice"] / m["chartPreviousClose"] - 1) * 100, 2)}
    except Exception as e:
        print(f"  [warn] brent: {e}")
        return None       # 取れなければ出さない。古い値を使い回さない。


# ══════════════════════════════════════════════════════════
# 重要度（自動採点。基準はこの関数そのもの＝アプリ上で公開する）
# ══════════════════════════════════════════════════════════
def severity(item) -> tuple[str, str]:
    if item.get("is_closure") and item["kind"] == "advisory":
        return "S", "当局が閉鎖に言及した警報。最優先。"
    if item.get("is_incident"):
        return "A", "船舶への実害。通航への影響が生じ得る。"
    if item["kind"] == "advisory":
        return "B", "航行警報。閉鎖の兆候ではない。"
    if item.get("is_closure"):
        return "B", "発言のみ。通航への影響は確認されていない。"
    return "C", "参考情報。"


# ══════════════════════════════════════════════════════════
# 判定（ルールベース。LLMは使わない）
# ══════════════════════════════════════════════════════════
def decide(adv7, inc30, closures, alive):
    if alive < 2:
        return 9, "確認中", "情報源を取得できていません。「開いている」とは断定できません。"
    if closures:
        return 4, "閉鎖", "当局が閉鎖に関する警報を発出しています。公的情報に従ってください。"
    if inc30 >= 3:
        return 3, "一部営業", f"過去30日に{inc30}件の事案。公的情報を優先してください。"
    if inc30 >= 1:
        return 2, "警戒", f"過去30日に{inc30}件の事案。閉鎖の報告はありません。"
    if adv7 >= 1:
        return 1, "営業中", f"過去7日に{adv7}件の航行警報。閉鎖の報告はありません。"
    return 0, "営業中", "どの当局も閉鎖を報告していません。航行警報・事案ともにゼロ。"


SUB = {
    0: "きょうも海峡は開いています。特にお伝えすることはありません",
    1: "通れます。ただし少し騒がしい",
    2: "通れます。ただし荒れています",
    3: "事案が続いています。公的情報を優先してください",
    4: "当局が閉鎖を報告しています。UKMTO / MARAD を確認してください",
    9: "「開いている」とは断定できません",
}
EN = {0: "B R E A K I N G · N O T H I N G", 1: "O P E N · 注意", 2: "O P E N · C A U T I O N",
      3: "D I S R U P T E D", 4: "C L O S E D", 9: "N O   D A T A"}


# ══════════════════════════════════════════════════════════
def history_update(level):
    h = json.loads(HISTORY.read_text()) if HISTORY.exists() else {}
    h[NOW.date().isoformat()] = level
    HISTORY.write_text(json.dumps(h, indent=0, sort_keys=True))
    days = [d for d, l in sorted(h.items()) if l == 4]
    start = days[-1] if days else min(h)      # 最後に閉鎖した日 or 稼働開始日
    return (NOW.date() - dt.date.fromisoformat(start)).days, len(h)


def main():
    print("collecting...")
    advisories, alive = collect_advisories()
    statements = collect_statements()

    adv7    = [a for a in advisories if _within(a, 7)]
    inc30   = [a for a in advisories if a["is_incident"] and _within(a, 30)]
    closures = [a for a in advisories if a["is_closure"] and _within(a, 7)]

    level, label, reason = decide(len(adv7), len(inc30), closures, alive)
    days_open, days_logged = history_update(level)

    feed = sorted(advisories + statements, key=lambda x: x["published"], reverse=True)[:40]
    for it in feed:
        it["sev"], it["sev_why"] = severity(it)

    latest = {}
    for side in ("iran", "usa"):
        s = [x for x in statements if x["side"] == side]
        latest[side] = s[0] if s else None      # 無ければ null → 画面は「発言なし」と出す

    status = {
        "level": level,
        "label": label,
        "en": EN[level],
        "sub": SUB[level],
        "reason": reason,
        "evidence": "通航実績（AIS）は計測していません。当局の警報・事案報告のみに基づく判定です。",
        "tiles": {
            "advisories_7d": len(adv7),
            "incidents_30d": len(inc30),
            "brent": fetch_brent(),
            "feeds_alive": f"{alive}/{len(ADVISORY_FEEDS)}",
        },
        "closure_talk_90d": sum(1 for s in statements if s["is_closure"] and _within(s, 90)),
        "days_no_closure": days_open,
        "days_logged": days_logged,
        "statements": latest,
        "feed": feed,
        "updated": NOW.isoformat(timespec="seconds"),
        "rules_url": "https://github.com/satamainuser/hormuz/blob/main/collect.py",
    }

    DOCS.mkdir(exist_ok=True)
    (DOCS / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2))
    CACHE.write_text(json.dumps(_cache, ensure_ascii=False, indent=0, sort_keys=True))
    print(f"LV{level} {label} — {reason}")
    print(f"advisories(7d)={len(adv7)} incidents(30d)={len(inc30)} feeds={alive}/3")


def _within(item, days):
    return dt.datetime.fromisoformat(item["published"]) > NOW - dt.timedelta(days=days)


if __name__ == "__main__":
    main()
