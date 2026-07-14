
"""
ホルムズ海峡ステータス — 収集スクリプト v2

【v1 の失敗】
  UKMTO / MARAD の RSS は存在しなかった（RSS を出していない）。
  NGA は RSS ではなく JSON API。
  → 3つとも空振りし、feeds_alive 0/3 → 全部「確認中」になっていた。
  ただし「取れないなら営業中と言わない」という設計のおかげで、
  嘘は一度もつかなかった。そこは壊さずに直す。

【v2 の構成】
  NGA      : JSON API（公式・安定）
  UKMTO    : サイトHTMLから抽出（RSSが無いため）
  MARAD    : サイトHTMLから抽出
  公式発言 : IRNA / PressTV / WhiteHouse / State（RSS。IRNAは v1 でも動いていた）
  Brent    : Yahoo Finance
  AIS      : aisstream.io（無料）で「いま海峡にいる商船」をスナップショット計測

【いまの現実に合わせた設計】
  2026年7月現在、海峡は当局により閉鎖が宣言されている。だが船はゼロではない。
  だからこのアプリが出すべきは：

      閉 鎖
      それでも34隻が通っています

  「閉鎖」は当局の宣言。「34隻」は現実。両方を並べる。
  どちらか一方だけを出すのは、どちらの側の宣伝にもなってしまう。
  ★ 通航数は必ず実測。推定・引用・手入力は禁止。取れなければ「計測できていません」。

env (GitHub Secrets):
  AISSTREAM_KEY      … https://aisstream.io で無料取得。無ければ通航数は出ない
  DEEPL_KEY          … 任意。無ければ翻訳せず原文のまま
  ANTHROPIC_API_KEY  … 任意（DeepLの予備）
"""

from __future__ import annotations
import os, re, json, html, asyncio, datetime as dt
from pathlib import Path

import httpx, feedparser

UTC = dt.timezone.utc
NOW = dt.datetime.now(UTC)
DOCS = Path(__file__).parent / "docs"
CACHE = DOCS / "translations.json"
HISTORY = DOCS / "history.json"

UA = {"User-Agent": "hormuz-status/2.0 (+https://github.com/satamainuser/hormuz)"}

AREA_WORDS     = ("hormuz", "persian gulf", "arabian gulf", "gulf of oman", "bandar abbas", "strait")
INCIDENT_WORDS = ("attack", "attacked", "seiz", "board", "hijack", "explos", "drone",
                  "missile", "struck", "mine", "fire")
CLOSURE_WORDS  = ("clos", "shut", "block", "blockad", "not possible", "suspend", "halt",
                  "no ship", "prohibit")


# ══════════════════════════════════════════════════════════
# 1. 航行警報
# ══════════════════════════════════════════════════════════
def get(url, **kw):
    try:
        r = httpx.get(url, headers=UA, timeout=25, follow_redirects=True, **kw)
        r.raise_for_status()
        return r
    except Exception as e:
        print(f"  [warn] {url} -> {e}")
        return None


def nga_warnings(days=30) -> list[dict]:
    """NGA MSI の JSON API。NAVAREA IX = インド洋・ペルシャ湾。
    ここが一番機械可読で安定している。"""
    out = []
    r = get("https://msi.nga.mil/api/publications/broadcast-warn",
            params={"navArea": "IX", "status": "active", "output": "json"})
    if not r:
        return out
    try:
        items = r.json().get("broadcast-warn", [])
    except Exception as e:
        print(f"  [warn] nga json: {e}")
        return out

    for w in items:
        text = (w.get("text") or "") + " " + (w.get("subregion") or "")
        if not any(k in text.lower() for k in AREA_WORDS):
            continue
        when = w.get("issueDate") or w.get("navAreaIssueDate")
        try:
            t = dt.datetime.strptime(when[:10], "%Y-%m-%d").replace(tzinfo=UTC)
        except Exception:
            t = NOW
        if t < NOW - dt.timedelta(days=days):
            continue
        title = clean(text)[:180]
        out.append(mk("advisory", "NGA", title,
                      f"https://msi.nga.mil/NavWarnings", t.isoformat()))
    return out


def scrape_titles(url, src, days=30) -> list[dict]:
    """UKMTO / MARAD は RSS が無いので、HTMLから見出しを拾う。
    構造が変わったら空を返すだけ（= feeds_alive が下がり、自動で確認中に落ちる）。"""
    r = get(url)
    if not r:
        return []
    # <a> のテキストのうち、警報らしいものだけ
    cands = re.findall(r"<a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>", r.text, re.S | re.I)
    out, seen = [], set()
    for href, raw in cands:
        title = clean(raw)
        if len(title) < 12 or title in seen:
            continue
        low = title.lower()
        if not any(k in low for k in AREA_WORDS):
            continue
        if not any(k in low for k in ("advisory", "warning", "incident", "alert",
                                      "attack", "update", "hormuz")):
            continue
        seen.add(title)
        link = href if href.startswith("http") else url.rstrip("/") + "/" + href.lstrip("/")
        out.append(mk("advisory", src, title, link, NOW.isoformat()))
    return out[:15]


def mk(kind, src, title, url, published, **extra):
    low = title.lower()
    d = {
        "kind": kind, "source": src,
        "title": title, "title_ja": translate(title),
        "url": url, "published": published,
        "is_incident": any(w in low for w in INCIDENT_WORDS),
        "is_closure":  any(w in low for w in CLOSURE_WORDS),
    }
    d.update(extra)
    return d


# ══════════════════════════════════════════════════════════
# 2. 公式発言（創作しない。見出しをそのまま + 機械翻訳）
# ══════════════════════════════════════════════════════════
STATEMENT_FEEDS = {
    "iran": [("IRNA", "https://en.irna.ir/rss"),
             ("Press TV", "https://www.presstv.ir/rss.xml"),
             ("Mehr", "https://en.mehrnews.com/rss")],
    "usa":  [("White House", "https://www.whitehouse.gov/news/feed/"),
             ("State Dept", "https://www.state.gov/rss-feed/press-releases/feed/"),
             ("CENTCOM", "https://www.centcom.mil/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=95")],
}


def statements(days=45) -> list[dict]:
    out = []
    since = NOW - dt.timedelta(days=days)
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
                              t.isoformat(), side=side))
    return sorted(out, key=lambda x: x["published"], reverse=True)


# ══════════════════════════════════════════════════════════
# 3. AIS —「いま海峡にいる商船」を実測する
#    ★ この数字だけは絶対に捏造しない。取れなければ null。
# ══════════════════════════════════════════════════════════
BBOX = [[25.6, 55.7], [27.1, 57.3]]     # ホルムズ海峡
CARGO_TYPES = set(range(70, 90))         # 70-79 貨物 / 80-89 タンカー


async def ais_snapshot(seconds=180) -> dict | None:
    """指定秒だけ AIS を購読し、海峡内にいた商船をユニークに数える。
    「24時間の通航数」ではなく「いま海峡にいる商船数」。
    ラベルもそう表示する。実測できるものだけを、実測した通りに出す。"""
    key = os.environ.get("AISSTREAM_KEY")
    if not key:
        print("  [info] AISSTREAM_KEY なし → 通航数は出さない")
        return None
    try:
        import websockets
    except ImportError:
        print("  [warn] websockets 未インストール")
        return None

    seen: dict[int, dict] = {}
    static: dict[int, int] = {}
    sub = {"APIKey": key, "BoundingBoxes": [BBOX],
           "FilterMessageTypes": ["PositionReport", "ShipStaticData"]}
    try:
        async with websockets.connect("wss://stream.aisstream.io/v0/stream",
                                      open_timeout=20) as ws:
            await ws.send(json.dumps(sub))
            end = NOW + dt.timedelta(seconds=seconds)
            while dt.datetime.now(UTC) < end:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=20)
                except asyncio.TimeoutError:
                    break
                m = json.loads(raw)
                mmsi = m.get("MetaData", {}).get("MMSI")
                if not mmsi:
                    continue
                if m["MessageType"] == "ShipStaticData":
                    static[mmsi] = m["Message"]["ShipStaticData"].get("Type", 0)
                elif m["MessageType"] == "PositionReport":
                    seen[mmsi] = m["Message"]["PositionReport"]
    except Exception as e:
        print(f"  [warn] ais: {e}")
        return None

    cargo = [m for m in seen if static.get(m, 0) in CARGO_TYPES]
    unknown = [m for m in seen if m not in static]
    return {
        "vessels_now": len(cargo),
        "unclassified": len(unknown),   # 船種不明はカウントに含めない（正直に別で出す）
        "window_sec": seconds,
        "method": "aisstream.io を{}秒購読し、海峡内で位置情報を発信していた商船（AIS船種70-89）をMMSIでユニーク集計".format(seconds),
    }


# ══════════════════════════════════════════════════════════
# 4. Brent
# ══════════════════════════════════════════════════════════
def brent():
    r = get("https://query1.finance.yahoo.com/v8/finance/chart/BZ=F")
    if not r:
        return None
    try:
        m = r.json()["chart"]["result"][0]["meta"]
        return {"price": round(m["regularMarketPrice"], 2),
                "change_pct": round((m["regularMarketPrice"] / m["chartPreviousClose"] - 1) * 100, 2)}
    except Exception as e:
        print(f"  [warn] brent parse: {e}")
        return None


# ══════════════════════════════════════════════════════════
# 5. 判定（ルールベース。LLMは使わない）
#
#   ★ 今回の肝：「閉鎖」と「通っている船」は両立する。
#     当局が閉鎖を宣言していても、船がゼロとは限らない。
#     宣言は宣言、実測は実測。両方を並べて出す。
# ══════════════════════════════════════════════════════════
def decide(alive, closures, incidents30, advisories7, ais):
    if alive < 1:
        return 9, "確認中", "情報源を取得できていません。「開いている」とは断定できません。"

    n = ais["vessels_now"] if ais else None

    if closures:
        if n == 0:
            return 4, "閉 鎖", "当局が閉鎖を宣言。海峡内に商船を確認できません。"
        if n:
            # ★ これが今の現実。宣言と実測が食い違っている。
            return 4, "閉 鎖", f"当局が閉鎖を宣言しています。ただし現在、海峡内に商船{n}隻を確認。"
        return 4, "閉 鎖", "当局が閉鎖を宣言しています。通航は計測できていません。"

    if incidents30 >= 3:
        return 3, "一部営業", f"過去30日に{incidents30}件の事案。閉鎖の宣言はありません。"
    if incidents30 >= 1:
        return 2, "警 戒", f"過去30日に{incidents30}件の事案。閉鎖の宣言はありません。"
    if advisories7 >= 1:
        return 1, "営業中", f"過去7日に{advisories7}件の航行警報。閉鎖の宣言はありません。"
    return 0, "営業中", "閉鎖の宣言はありません。航行警報・事案ともにゼロ。"


def subtitle(level, ais):
    n = ais["vessels_now"] if ais else None
    if level == 9:
        return "「開いている」とは断定できません"
    if level == 4:
        if n:
            return f"それでも{n}隻が海峡にいます"          # ← 現実は現実として出す
        if n == 0:
            return "海峡に商船を確認できません。UKMTO / MARAD を確認してください"
        return "通航は計測できていません"
    if n is not None:
        return f"いま海峡に商船{n}隻"
    return "通航は計測していません（AIS未接続）"


# ══════════════════════════════════════════════════════════
# 翻訳
# ══════════════════════════════════════════════════════════
_cache: dict[str, str] = json.loads(CACHE.read_text()) if CACHE.exists() else {}


def translate(text: str) -> str:
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
    host = "api-free" if key.endswith(":fx") else "api"
    try:
        r = httpx.post(f"https://{host}.deepl.com/v2/translate",
                       data={"auth_key": key, "text": text, "target_lang": "JA"}, timeout=20)
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
                                        "意訳・要約・脚色・誇張をしない。断定の強さを変えない。"),
                             "messages": [{"role": "user", "content": text}]},
                       timeout=30)
        r.raise_for_status()
        return r.json()["content"][0]["text"].strip()
    except Exception as e:
        print(f"  [warn] claude: {e}")
        return None


# ══════════════════════════════════════════════════════════
def entries(url):
    try:
        f = feedparser.parse(url, agent=UA["User-Agent"])
        return f.entries or []
    except Exception as e:
        print(f"  [warn] feed {url}: {e}")
        return []


def when(e):
    for k in ("published_parsed", "updated_parsed"):
        if e.get(k):
            return dt.datetime(*e[k][:6], tzinfo=UTC)
    return None


def clean(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def within(item, days):
    try:
        return dt.datetime.fromisoformat(item["published"]) > NOW - dt.timedelta(days=days)
    except Exception:
        return True


def severity(i):
    if i["is_closure"] and i["kind"] == "advisory":
        return "S", "当局が閉鎖に言及した警報。最優先。"
    if i["is_incident"]:
        return "A", "船舶への実害。通航への影響が生じ得る。"
    if i["kind"] == "advisory":
        return "B", "航行警報。"
    if i["is_closure"]:
        return "B", "発言のみ。実際の通航状況とは別。"
    return "C", "参考情報。"


def history(level):
    h = json.loads(HISTORY.read_text()) if HISTORY.exists() else {}
    h[NOW.date().isoformat()] = level
    HISTORY.write_text(json.dumps(h, indent=0, sort_keys=True))
    closed = [d for d, l in sorted(h.items()) if l == 4]
    start = closed[-1] if closed else min(h)
    return (NOW.date() - dt.date.fromisoformat(start)).days, len(h)


def main():
    print("collecting...")

    advisories = []
    alive = 0
    for name, fn in [
        ("NGA",   lambda: nga_warnings()),
        ("UKMTO", lambda: scrape_titles("https://www.ukmto.org/", "UKMTO")),
        ("MARAD", lambda: scrape_titles("https://www.maritime.dot.gov/msci-advisories", "MARAD")),
    ]:
        got = fn()
        if got:
            alive += 1
        print(f"  {name}: {len(got)}")
        advisories += got

    stmts = statements()
    if stmts:
        alive += 1
    print(f"  statements: {len(stmts)}")

    ais = asyncio.run(ais_snapshot())
    print(f"  ais: {ais}")

    adv7  = [a for a in advisories if within(a, 7)]
    inc30 = [a for a in advisories if a["is_incident"] and within(a, 30)]
    # 閉鎖の宣言は「当局の警報」だけでなく「政府の公式発表」も見る
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
        "label": label,
        "en": {0: "O P E N", 1: "O P E N", 2: "C A U T I O N",
               3: "D I S R U P T E D", 4: "C L O S E D", 9: "N O   D A T A"}[level],
        "sub": subtitle(level, ais),
        "reason": reason,
        "evidence": (ais["method"] if ais else
                     "通航は計測していません（AIS未接続）。当局の警報・宣言のみに基づく判定です。"),
        "ais": ais,                       # null なら通航数は画面に出さない
        "tiles": {
            "vessels_now": ais["vessels_now"] if ais else None,
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
    CACHE.write_text(json.dumps(_cache, ensure_ascii=False, indent=0, sort_keys=True))

    print(f"\nLV{level} {label} — {reason}")
    print(f"feeds={alive}/4  adv7={len(adv7)}  inc30={len(inc30)}  closures={len(closures)}")


if __name__ == "__main__":
    main()
