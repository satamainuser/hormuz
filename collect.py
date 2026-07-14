"""
ホルムズ海峡ステータス — collect.py（最終版）

設計方針：どこか1つでも死んでいて動く。全部死んだら「確認中」と言う。それだけ。

  ・すべての情報源は「取れたら使う、取れなければ黙って捨てる」
  ・どの情報源も、失敗してもスクリプトは止まらない
  ・情報源がゼロなら level=9「確認中」。「営業中」とは絶対に言わない
  ・AISの数字は「AISを発信している船の数」であって「海峡にいる船の数」ではない
    （紛争下では船はAISを切る。0隻＝船がいない、ではない）

【v2 までの失敗と、その対処】
  UKMTO / MARAD  … CDNが GitHub のIPを 403 で弾く → 直接取得は諦め、報道で代替
  NGA            … RSS ではなく JSON API。複数の呼び方を順に試す
  AIS            … 無言で失敗していた → サーバーの返事をログに出す

env（すべて任意。無くても動く）:
  AISSTREAM_KEY / DEEPL_KEY / ANTHROPIC_API_KEY
"""

from __future__ import annotations
import os, re, json, html, asyncio, traceback, datetime as dt
from pathlib import Path

import httpx, feedparser

UTC = dt.timezone.utc
NOW = dt.datetime.now(UTC)
DOCS = Path(__file__).parent / "docs"
CACHE = DOCS / "translations.json"
HISTORY = DOCS / "history.json"

UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

AREA_WORDS     = ("hormuz", "persian gulf", "arabian gulf", "gulf of oman", "bandar abbas")
INCIDENT_WORDS = ("attack", "attacked", "seiz", "board", "hijack", "explos", "drone",
                  "missile", "struck", "strike", "mine", "fire", "damag")
CLOSURE_WORDS  = ("clos", "shut", "block", "blockad", "not possible", "suspend",
                  "halt", "no ship", "prohibit", "ban ")


# ══════════════════════════════════════════════════════════
# 安全装置：どの収集も、失敗してスクリプトを止めない
# ══════════════════════════════════════════════════════════
def safe(name, fn, default):
    try:
        out = fn()
        print(f"  {name}: {len(out) if hasattr(out, '__len__') else out}")
        return out
    except Exception as e:
        print(f"  [warn] {name} 失敗: {type(e).__name__}: {e}")
        return default


def get(url, **kw):
    try:
        r = httpx.get(url, headers=UA, timeout=25, follow_redirects=True, **kw)
        r.raise_for_status()
        return r
    except Exception as e:
        print(f"  [warn] GET {url[:70]} -> {type(e).__name__}")
        return None


def entries(url):
    try:
        f = feedparser.parse(url, agent=UA["User-Agent"])
        return f.entries or []
    except Exception:
        return []


def clean(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def when(e):
    for k in ("published_parsed", "updated_parsed"):
        if e.get(k):
            try:
                return dt.datetime(*e[k][:6], tzinfo=UTC)
            except Exception:
                pass
    return None


def mk(kind, src, title, url, published, **extra):
    low = (title or "").lower()
    d = {"kind": kind, "source": src, "title": title, "title_ja": translate(title),
         "url": url or "", "published": published,
         "is_incident": any(w in low for w in INCIDENT_WORDS),
         "is_closure":  any(w in low for w in CLOSURE_WORDS)}
    d.update(extra)
    return d


def within(item, days):
    try:
        return dt.datetime.fromisoformat(item["published"]) > NOW - dt.timedelta(days=days)
    except Exception:
        return True


# ══════════════════════════════════════════════════════════
# 情報源 1：NGA（一次情報・公式）
# ══════════════════════════════════════════════════════════
def nga(days=45):
    out = []
    for params in (
        {"navArea": "IX", "status": "active", "output": "json"},
        {"status": "active", "output": "json"},
    ):
        r = get("https://msi.nga.mil/api/publications/broadcast-warn", params=params)
        if not r:
            continue
        try:
            items = r.json().get("broadcast-warn", [])
        except Exception:
            continue
        for w in items:
            text = f"{w.get('text','')} {w.get('subregion','')}"
            if not any(k in text.lower() for k in AREA_WORDS):
                continue
            raw = (w.get("issueDate") or "")[:10]
            try:
                t = dt.datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC)
            except Exception:
                t = NOW
            if t < NOW - dt.timedelta(days=days):
                continue
            out.append(mk("advisory", "NGA", clean(text)[:160],
                          "https://msi.nga.mil/NavWarnings", t.isoformat()))
        if out:
            break
    return out


# ══════════════════════════════════════════════════════════
# 情報源 2：報道（二次情報。ソース名に「報道」と明記する）
#   UKMTO / MARAD は CDN が弾くので直接取れない。
#   「当局が何を出したか」を報じる記事で代替する。一次情報のふりはしない。
# ══════════════════════════════════════════════════════════
NEWS = [
    "https://news.google.com/rss/search?q=%22Strait+of+Hormuz%22+when:7d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=Hormuz+shipping+OR+UKMTO+OR+MARAD+when:14d&hl=en-US&gl=US&ceid=US:en",
]


def news(days=14):
    out, seen = [], set()
    since = NOW - dt.timedelta(days=days)
    for url in NEWS:
        for e in entries(url):
            title = clean(e.get("title"))
            if not title or title in seen or "hormuz" not in title.lower():
                continue
            t = when(e) or NOW
            if t < since:
                continue
            seen.add(title)
            out.append(mk("advisory", "報道", title, e.get("link"), t.isoformat()))
    return out[:25]


# ══════════════════════════════════════════════════════════
# 情報源 3：公式発言（吹き出し。創作しない）
# ══════════════════════════════════════════════════════════
STATEMENT_FEEDS = {
    "iran": [("IRNA", "https://en.irna.ir/rss"),
             ("Press TV", "https://www.presstv.ir/rss.xml"),
             ("Mehr", "https://en.mehrnews.com/rss")],
    "usa":  [("White House", "https://www.whitehouse.gov/news/feed/"),
             ("State Dept", "https://www.state.gov/rss-feed/press-releases/feed/")],
}


# 米側の発言は公式RSSに載らないことが多い（Truth Social は機械取得できない）。
# 報道からも拾う。ただし「報道による発言」であることをソース名に明記する。
SPEAKER_NEWS = [
  "https://news.google.com/rss/search?q=Trump+OR+%22White+House%22+OR+Pentagon+Hormuz+when:30d&hl=en-US&gl=US&ceid=US:en",
  "https://news.google.com/rss/search?q=Iran+OR+IRGC+OR+Araghchi+Hormuz+statement+when:30d&hl=en-US&gl=US&ceid=US:en",
]
US_WORDS   = ("trump", "white house", "pentagon", "centcom", "u.s.", "us ", "american", "washington")
IRAN_WORDS = ("iran", "irgc", "tehran", "araghchi", "khamenei", "pezeshkian", "revolutionary guard")


def side_of(title):
    low = title.lower()
    us  = any(w in low for w in US_WORDS)
    ira = any(w in low for w in IRAN_WORDS)
    if us and not ira:
        return "usa"
    if ira and not us:
        return "iran"
    # 両方出てくる場合は、先に出てきたほうを話者とみなす
    iu = min([low.find(w) for w in US_WORDS if w in low] or [9999])
    ii = min([low.find(w) for w in IRAN_WORDS if w in low] or [9999])
    return "usa" if iu < ii else ("iran" if ii < 9999 else None)


def statements(days=45):
    out = []
    since = NOW - dt.timedelta(days=days)

    # ① 公式フィード（一次情報）
    for side, feeds in STATEMENT_FEEDS.items():
        for name, url in feeds:
            for e in entries(url):
                title = clean(e.get("title"))
                blob = (title + " " + clean(e.get("summary"))).lower()
                if "hormuz" not in blob:
                    continue
                t = when(e)
                if not t or t < since:
                    continue
                out.append(mk("statement", name, title, e.get("link"),
                              t.isoformat(), side=side, primary=True))

    # ② 報道（二次情報）。一次情報が無い側を埋める
    seen = {x["title"] for x in out}
    for url in SPEAKER_NEWS:
        for e in entries(url):
            title = clean(e.get("title"))
            if not title or title in seen or "hormuz" not in title.lower():
                continue
            t = when(e)
            if not t or t < since:
                continue
            sd = side_of(title)
            if not sd:
                continue
            seen.add(title)
            out.append(mk("statement", "報道", title, e.get("link"),
                          t.isoformat(), side=sd, primary=False))

    return sorted(out, key=lambda x: x["published"], reverse=True)


# ══════════════════════════════════════════════════════════
# 情報源 4：AIS
#   ★ これは「海峡にいる船の数」ではない。「AISを発信している船の数」である。
#     紛争下では船はAISを切る。0隻は「船がいない」ことを意味しない。
# ══════════════════════════════════════════════════════════
CARGO_TYPES = set(range(70, 90))


async def _ais(seconds):
    key = os.environ.get("AISSTREAM_KEY")
    if not key:
        print("  [info] AISSTREAM_KEY なし → AISは使わない")
        return None
    try:
        import websockets
    except ImportError:
        print("  [warn] websockets 未インストール")
        return None

    sub = {
        "APIKey": key.strip(),
        "BoundingBoxes": [[[25.4, 55.4], [27.3, 57.6]]],   # [[SW],[NE]]
        "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
    }
    seen, static, msgs = {}, {}, 0

    async with websockets.connect("wss://stream.aisstream.io/v0/stream",
                                  open_timeout=30, ping_interval=20) as ws:
        await ws.send(json.dumps(sub))
        end = dt.datetime.now(UTC) + dt.timedelta(seconds=seconds)
        while dt.datetime.now(UTC) < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=60)
            except asyncio.TimeoutError:
                print("  [warn] AIS: 60秒受信なし")
                break
            try:
                m = json.loads(raw)
            except Exception:
                print(f"  [warn] AIS 生の返答: {str(raw)[:160]}")
                return None
            if "MessageType" not in m:
                # サーバーからのエラー（キー不正など）はここに来る。黙って捨てない。
                print(f"  [warn] AISサーバーの返答: {str(m)[:160]}")
                return None
            msgs += 1
            mmsi = m.get("MetaData", {}).get("MMSI")
            if not mmsi:
                continue
            if m["MessageType"] == "ShipStaticData":
                static[mmsi] = m["Message"]["ShipStaticData"].get("Type", 0)
            else:
                seen[mmsi] = 1

    cargo = [m for m in seen if static.get(m, 0) in CARGO_TYPES]
    print(f"  [info] AIS messages={msgs} 全船={len(seen)} 商船={len(cargo)}")
    return {
        "ais_visible_cargo": len(cargo),
        "ais_visible_any": len(seen),
        "window_sec": seconds,
        "method": f"aisstream.io を{seconds}秒購読し、海峡内でAISを発信していた商船（船種70-89）をMMSIでユニーク集計。",
        "caveat": ("AISは船が自分で発信するもので、義務ではありません。紛争下では攻撃を避けるため"
                   "AISを切る船が多く、この数字は「海峡にいる船の数」ではなく"
                   "「AISを発信している船の数」です。"),
    }


def ais_snapshot(seconds=150):
    try:
        return asyncio.run(_ais(seconds))
    except Exception as e:
        print(f"  [warn] AIS 失敗: {type(e).__name__}: {e}")
        return None


# ══════════════════════════════════════════════════════════
# Brent
# ══════════════════════════════════════════════════════════
def brent():
    r = get("https://query1.finance.yahoo.com/v8/finance/chart/BZ=F")
    if not r:
        return None
    try:
        m = r.json()["chart"]["result"][0]["meta"]
        return {"price": round(m["regularMarketPrice"], 2),
                "change_pct": round((m["regularMarketPrice"] / m["chartPreviousClose"] - 1) * 100, 2)}
    except Exception:
        return None


# ══════════════════════════════════════════════════════════
# 翻訳（失敗したら原文のまま。勝手な意訳より安全）
# ══════════════════════════════════════════════════════════
try:
    _cache = json.loads(CACHE.read_text())
except Exception:
    _cache = {}


def translate(text):
    text = (text or "").strip()
    if not text:
        return ""
    if text in _cache:
        return _cache[text]
    ja = _deepl(text) or _claude(text) or text
    _cache[text] = ja
    return ja


def _deepl(text):
    key = os.environ.get("DEEPL_KEY")
    if not key:
        return None
    host = "api-free" if key.strip().endswith(":fx") else "api"
    try:
        r = httpx.post(f"https://{host}.deepl.com/v2/translate",
                       data={"auth_key": key.strip(), "text": text, "target_lang": "JA"},
                       timeout=20)
        r.raise_for_status()
        return r.json()["translations"][0]["text"]
    except Exception:
        return None


def _claude(text):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        r = httpx.post("https://api.anthropic.com/v1/messages",
                       headers={"x-api-key": key.strip(), "anthropic-version": "2023-06-01",
                                "content-type": "application/json"},
                       json={"model": "claude-haiku-4-5-20251001", "max_tokens": 300,
                             "system": ("ニュース見出しを日本語に訳す。訳文だけを返す。"
                                        "意訳・要約・脚色・誇張をしない。断定の強さを変えない。"),
                             "messages": [{"role": "user", "content": text}]},
                       timeout=30)
        r.raise_for_status()
        return r.json()["content"][0]["text"].strip()
    except Exception:
        return None


# ══════════════════════════════════════════════════════════
# 判定（ルールベース。LLMは使わない）
#
#   ★ 「閉鎖の宣言」と「実際に船が通っているか」は別の話。
#     当局が閉鎖と言っていても、船はゼロとは限らない。
#     宣言は宣言、実測は実測。両方を並べて出す。
#     片方だけ出したら、どちらかの側の宣伝になる。
# ══════════════════════════════════════════════════════════
def decide(alive, closures, inc30, adv7, ais):
    if alive < 1:
        return 9, "確認中", "情報源を取得できていません。「開いている」とは断定できません。"

    n = ais["ais_visible_cargo"] if ais else None

    if closures:
        if n:
            return 4, "閉 鎖", f"当局が閉鎖を宣言。ただしAIS上、商船{n}隻が海峡内にいます。"
        if n == 0:
            return 4, "閉 鎖", ("当局が閉鎖を宣言。AISを発信している商船はゼロですが、"
                               "これは「船がいない」ことを意味しません（AISは切れます）。")
        return 4, "閉 鎖", "当局が閉鎖を宣言しています。通航は計測できていません。"

    if inc30 >= 3:
        return 3, "一部営業", f"過去30日に{inc30}件の事案。閉鎖の宣言はありません。"
    if inc30 >= 1:
        return 2, "警 戒", f"過去30日に{inc30}件の事案。閉鎖の宣言はありません。"
    if adv7 >= 1:
        return 1, "営業中", f"過去7日に{adv7}件の警報・報道。閉鎖の宣言はありません。"
    return 0, "営業中", "閉鎖の宣言はありません。警報・事案ともにゼロ。"


def subtitle(level, ais):
    n = ais["ais_visible_cargo"] if ais else None
    if level == 9:
        return "「開いている」とは断定できません"
    if level == 4:
        if n:
            return f"それでも{n}隻がAIS上を進んでいます"
        if n == 0:
            return "AISを発信している船はゼロ。ただし船はAISを切れます"
        return "通航は計測できていません"
    if n:
        return f"AIS上、いま海峡に商船{n}隻"
    if n == 0:
        return "AISを発信している商船はゼロです"
    return "通航は計測していません（AIS未接続）"


def severity(i):
    if i["is_closure"] and i["kind"] == "advisory":
        return "S", "閉鎖に言及した警報・報道。最優先。"
    if i["is_incident"]:
        return "A", "船舶への実害。通航への影響が生じ得る。"
    if i["kind"] == "advisory":
        return "B", "警報・報道。"
    if i["is_closure"]:
        return "B", "発言のみ。実際の通航状況とは別。"
    return "C", "参考情報。"


def history(level):
    try:
        h = json.loads(HISTORY.read_text())
    except Exception:
        h = {}
    h[NOW.date().isoformat()] = level
    HISTORY.write_text(json.dumps(h, indent=0, sort_keys=True))
    closed = [d for d, l in sorted(h.items()) if l == 4]
    start = closed[-1] if closed else min(h)
    try:
        return (NOW.date() - dt.date.fromisoformat(start)).days, len(h)
    except Exception:
        return 0, len(h)


# ══════════════════════════════════════════════════════════
def main():
    print("collecting...")

    advisories = []
    alive = 0

    got = safe("NGA", nga, [])
    if got:
        alive += 1
    advisories += got

    got = safe("報道", news, [])
    if got:
        alive += 1
    advisories += got

    stmts = safe("公式発言", statements, [])
    if stmts:
        alive += 1

    ais = ais_snapshot()
    if ais:
        alive += 1

    adv7  = [a for a in advisories if within(a, 7)]
    inc30 = [a for a in advisories if a["is_incident"] and within(a, 30)]
    closures = [x for x in advisories + stmts if x["is_closure"] and within(x, 7)]

    level, label, reason = decide(alive, closures, len(inc30), len(adv7), ais)
    days_since, days_logged = history(level)

    feed = sorted(advisories + stmts, key=lambda x: x["published"], reverse=True)[:40]
    for it in feed:
        it["sev"], it["sev_why"] = severity(it)

    latest = {}
    for side in ("iran", "usa"):
        s = [x for x in stmts if x.get("side") == side]
        latest[side] = s[0] if s else None

    status = {
        "level": level,
        "level_display": (None if level == 9 else level + 1),   # 平常=1 … 閉鎖=5
        "label": label,
        "en": {0: "O P E N", 1: "O P E N", 2: "C A U T I O N", 3: "D I S R U P T E D",
               4: "C L O S E D", 9: "N O   D A T A"}[level],
        "sub": subtitle(level, ais),
        "reason": reason,
        "evidence": ((ais["method"] + ais["caveat"]) if ais else
                     "通航は計測していません（AIS未接続）。当局の警報・宣言・報道のみに基づく判定です。"),
        "ais": ais,
        "tiles": {
            "ais_visible_cargo": ais["ais_visible_cargo"] if ais else None,
            "advisories_7d": len(adv7),
            "incidents_30d": len(inc30),
            "brent": brent(),
            "feeds_alive": f"{alive}/4",
        },
        "closure_talk_90d": sum(1 for s in stmts if s["is_closure"] and within(s, 90)),
        "days_since_change": days_since,
        "days_logged": days_logged,
        "statements": latest,
        "feed": feed,
        "updated": NOW.isoformat(timespec="seconds"),
        "rules_url": "https://github.com/satamainuser/hormuz/blob/main/collect.py",
    }

    DOCS.mkdir(exist_ok=True)
    (DOCS / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2))
    try:
        CACHE.write_text(json.dumps(_cache, ensure_ascii=False, indent=0, sort_keys=True))
    except Exception:
        pass

    print(f"\nLV{level} {label} — {reason}")
    print(f"feeds={alive}/4  adv7={len(adv7)}  inc30={len(inc30)}  closures={len(closures)}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # ここまで来て落ちても、status.json は絶対に古いまま放置しない
        traceback.print_exc()
        DOCS.mkdir(exist_ok=True)
        (DOCS / "status.json").write_text(json.dumps({
            "level": 9, "label": "確認中", "en": "N O   D A T A",
            "sub": "「開いている」とは断定できません",
            "reason": "収集処理が失敗しました。",
            "evidence": "情報を取得できていません。",
            "ais": None,
            "tiles": {"ais_visible_cargo": None, "advisories_7d": 0, "incidents_30d": 0,
                      "brent": None, "feeds_alive": "0/4"},
            "closure_talk_90d": 0, "days_since_change": 0, "days_logged": 0,
            "statements": {"iran": None, "usa": None}, "feed": [],
            "updated": NOW.isoformat(timespec="seconds"),
            "rules_url": "https://github.com/satamainuser/hormuz/blob/main/collect.py",
        }, ensure_ascii=False, indent=2))
        raise SystemExit(0)   # ワークフローは緑のまま。だが画面は「確認中」になる。
