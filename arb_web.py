"""Arbitraj Tarayici - daankoning/ArbitrageFinder'in GitHub Actions + telefon uyumlu surumu.

The Odds API'den oranlari ceker, ayni macta farkli bahis sirketlerinin en iyi
oranlarini birlestirir ve toplam ima edilen olasilik 1'in altindaysa (arbitraj)
bunu docs/index.html sayfasina ve docs/arbs.json dosyasina yazar.

Ek kurulum gerektirmez: sadece Python'un kendi kutuphanelerini kullanir.
Ayarlar ortam degiskenleriyle yapilir (workflow dosyasinda):
  API_KEY   The Odds API anahtari (GitHub Secret olarak)
  REGION    eu / uk / us / au            (varsayilan: eu)
  SPORTS    virgulle lig anahtarlari ya da "all" (varsayilan: asagidaki liste)
  CUTOFF    en az kar yuzdesi, orn. 0.5  (varsayilan: 0)
  STAKE     ornek toplam yatirim          (varsayilan: 1000)
  RESERVE   kalan kredi bunun altina dusunce durur (varsayilan: 15)
"""
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://api.the-odds-api.com/v4"
TR = timezone(timedelta(hours=3))

DEFAULT_SPORTS = [
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_germany_bundesliga",
    "soccer_france_ligue_one",
    "soccer_turkey_super_league",
    "soccer_uefa_champs_league",
    "basketball_euroleague",
]

KEY = os.environ.get("API_KEY", "").strip()
REGION = os.environ.get("REGION", "eu").strip() or "eu"
SPORTS_ENV = os.environ.get("SPORTS", "").strip()
CUTOFF = float(os.environ.get("CUTOFF", "0") or 0) / 100
STAKE = float(os.environ.get("STAKE", "1000") or 1000)
RESERVE = int(os.environ.get("RESERVE", "15") or 15)
OUT_DIR = "docs"

quota = {"remaining": None, "used": None}


def api_get(path, params):
    params = dict(params, apiKey=KEY)
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "arb-web/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            rem = r.headers.get("x-requests-remaining")
            used = r.headers.get("x-requests-used")
            if rem is not None:
                quota["remaining"] = int(float(rem))
            if used is not None:
                quota["used"] = int(float(used))
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        if e.code == 401:
            raise SystemExit("HATA: API anahtari gecersiz. GitHub Secret 'API_KEY' degerini kontrol et.")
        if e.code == 429:
            raise SystemExit("HATA: API kotasi doldu ya da cok hizli istek atildi. Ayin 1'inde kota yenilenir.")
        print(f"UYARI: {path} icin hata {e.code}: {body[:200]}")
        return None


def pick_sports():
    active = api_get("/sports", {}) or []  # bu cagri kotadan yemez
    active_keys = {s["key"]: s for s in active if not s.get("has_outrights")}
    if SPORTS_ENV.lower() == "all":
        wanted = list(active_keys)
    else:
        wanted = [s.strip() for s in SPORTS_ENV.split(",") if s.strip()] or DEFAULT_SPORTS
    chosen = [s for s in wanted if s in active_keys]
    skipped = [s for s in wanted if s not in active_keys]
    if skipped:
        print("Sezonda olmadigi icin atlandi:", ", ".join(skipped))
    return chosen, active_keys


def analyse(match, now):
    start = int(match["commence_time"])
    if start <= now:
        return None  # baslamis mac: canli oranlar gecikmeli, sahte arbitraj uretir
    best = {}
    outcome_count = 0
    for bm in match.get("bookmakers", []):
        market = next((m for m in bm.get("markets", []) if m.get("key") == "h2h"), None)
        if not market:
            continue
        outcomes = market.get("outcomes", [])
        outcome_count = max(outcome_count, len(outcomes))
        for o in outcomes:
            name, price = o["name"], float(o["price"])
            if price <= 1:
                continue
            if name not in best or price > best[name]["odd"]:
                best[name] = {
                    "odd": price,
                    "bookmaker": bm["title"],
                    "updated": bm.get("last_update"),
                }
    # Tum sonuclar (orn. futbolda 1-X-2) karsilanmadiysa bu arbitraj degildir
    if match["sport_key"].startswith("soccer"):
        outcome_count = max(outcome_count, 3)  # futbolda beraberlik yoksa sahte arbitraj olur
    if outcome_count < 2 or len(best) < outcome_count:
        return None
    total = sum(1 / v["odd"] for v in best.values())
    if not (0 < total < 1 - CUTOFF):
        return None
    profit_pct = (1 / total - 1) * 100
    legs = []
    for name, v in best.items():
        stake = STAKE * (1 / v["odd"]) / total
        legs.append({
            "outcome": name,
            "bookmaker": v["bookmaker"],
            "odd": v["odd"],
            "stake": round(stake, 2),
            "return": round(stake * v["odd"], 2),
            "updated": v["updated"],
        })
    return {
        "match": f"{match['home_team']} - {match['away_team']}",
        "league": match.get("sport_title") or match["sport_key"],
        "sport_key": match["sport_key"],
        "start": start,
        "total_implied": round(total, 4),
        "profit_pct": round(profit_pct, 2),
        "suspicious": profit_pct > 5,
        "legs": legs,
    }


def fmt_time(ts):
    if ts is None:
        return "-"
    if isinstance(ts, str):
        try:
            ts = int(ts)
        except ValueError:
            return ts
    return datetime.fromtimestamp(ts, TR).strftime("%d.%m %H:%M")


def money(x):
    return f"{x:,.2f}".replace(",", " ").replace(".", ",")


def render(arbs, scanned, sports, now):
    rows = []
    for a in arbs:
        colors = ["#2f6fdb", "#e0a526", "#8a5cd6"]
        bar = "".join(
            f'<span style="flex:{l["stake"]};background:{colors[i % 3]}"></span>'
            for i, l in enumerate(a["legs"])
        )
        legs = "".join(
            f'<tr><td><i style="background:{colors[i % 3]}"></i>{html.escape(l["outcome"])}</td>'
            f'<td>{html.escape(l["bookmaker"])}</td><td class="n">{l["odd"]:.2f}</td>'
            f'<td class="n">{money(l["stake"])}</td></tr>'
            for i, l in enumerate(a["legs"])
        )
        payout = min(l["return"] for l in a["legs"])
        warn = ('<p class="warn">Kâr %5\'in üstünde: oranlardan biri büyük ihtimalle hatalı, eski '
                'ya da kapanmak üzere. Oynamadan önce iki sitede de elle kontrol et.</p>'
                if a["suspicious"] else "")
        rows.append(f"""
<article>
  <header>
    <div><h2>{html.escape(a['match'])}</h2>
    <p class="meta">{html.escape(a['league'])}, başlama {fmt_time(a['start'])}</p></div>
    <div class="pct">+{a['profit_pct']:.2f}%</div>
  </header>
  <div class="bar">{bar}</div>
  <div class="scroll"><table>
    <thead><tr><th>Sonuç</th><th>Şirket</th><th class="n">Oran</th><th class="n">Yatır</th></tr></thead>
    <tbody>{legs}</tbody>
  </table></div>
  <p class="sum">Toplam {money(STAKE)} yatırırsan hangi sonuç gelirse gelsin en az
  <b>{money(payout)}</b> geri alırsın (net ≈ {money(payout - STAKE)}).</p>
  {warn}
</article>""")
    body = "".join(rows) or """
<div class="empty"><h2>Şu an arbitraj yok</h2>
<p>Bu normal: gerçek fırsatlar nadirdir ve dakikalar içinde kapanır. Sayfa her taramada kendiliğinden güncellenir;
beklemek istemezsen GitHub'da Actions sekmesinden taramayı elle başlatabilirsin.</p></div>"""
    rem = quota["remaining"]
    return f"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Arbitraj Tarayıcı</title>
<style>
:root{{--bg:#eef1f5;--card:#fff;--ink:#16213a;--mute:#5d6883;--line:#d7dde8;--win:#11845b;--warn:#a1480f;--warnbg:#fff1e3;
  box-sizing:border-box;padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f1626;--card:#18223a;--ink:#e8edf7;--mute:#9aa6c2;--line:#2a3654;--win:#38c98f;--warn:#ffb77a;--warnbg:#3a2616}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}}
main{{max-width:680px;margin:0 auto;padding:20px 14px 40px}}
h1{{font-size:1.6rem;margin:0 0 4px;letter-spacing:-.01em}}
.top p{{margin:0;color:var(--mute);font-size:.9rem}}
.stats{{display:flex;gap:18px;flex-wrap:wrap;margin:14px 0 22px;font-size:.9rem;color:var(--mute)}}
.stats b{{color:var(--ink);font-size:1.05rem}}
article,.empty{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:14px}}
article header{{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}}
h2{{font-size:1.05rem;margin:0}}
.meta{{margin:2px 0 0;color:var(--mute);font-size:.85rem}}
.pct{{color:var(--win);font-weight:700;font-size:1.5rem;white-space:nowrap}}
.bar{{display:flex;height:8px;border-radius:4px;overflow:hidden;margin:12px 0 6px;gap:2px}}
.scroll{{overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:.9rem}}
th{{text-align:left;color:var(--mute);font-weight:500;padding:6px 4px;border-bottom:1px solid var(--line)}}
td{{padding:7px 4px;border-bottom:1px solid var(--line)}}
tr:last-child td{{border-bottom:0}}
.n{{text-align:right;font-variant-numeric:tabular-nums}}
td i{{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:6px}}
.sum{{margin:10px 0 0;font-size:.9rem}}
.warn{{margin:10px 0 0;padding:8px 10px;border-radius:8px;background:var(--warnbg);color:var(--warn);font-size:.85rem}}
.empty p{{color:var(--mute);margin:6px 0 0}}
footer{{color:var(--mute);font-size:.8rem;margin-top:24px}}
</style></head><body><main>
<div class="top"><h1>Arbitraj Tarayıcı</h1>
<p>Son tarama {datetime.fromtimestamp(now, TR).strftime('%d.%m.%Y %H:%M')} (TR saati), bölge {REGION.upper()}</p></div>
<div class="stats"><span><b>{len(arbs)}</b> fırsat</span><span><b>{scanned}</b> maç tarandı</span>
<span><b>{len(sports)}</b> lig</span><span><b>{rem if rem is not None else '-'}</b> kredi kaldı</span></div>
{body}
<footer>Oranlar The Odds API'den gelir ve birkaç dakika gecikmeli olabilir. Oynamadan önce her oranı sitede kontrol et.
Kaynak: daankoning/ArbitrageFinder (GPL-2.0).</footer>
</main></body></html>"""


def main():
    if not KEY:
        raise SystemExit("HATA: API_KEY bulunamadi. GitHub > Settings > Secrets and variables > Actions > API_KEY ekle.")
    now = int(time.time())
    sports, _ = pick_sports()
    print(f"Taranacak ligler ({len(sports)}): {', '.join(sports)}")
    arbs, scanned = [], 0
    for sp in sports:
        if quota["remaining"] is not None and quota["remaining"] < RESERVE:
            print(f"Kalan kredi {quota['remaining']} < {RESERVE}, tarama durduruldu.")
            break
        data = api_get(f"/sports/{sp}/odds/", {
            "regions": REGION, "markets": "h2h", "oddsFormat": "decimal", "dateFormat": "unix",
        })
        if not isinstance(data, list):
            continue
        for m in data:
            scanned += 1
            r = analyse(m, now)
            if r:
                arbs.append(r)
    arbs.sort(key=lambda a: -a["profit_pct"])
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "arbs.json"), "w", encoding="utf-8") as f:
        json.dump({"scanned_at": now, "region": REGION, "sports": sports, "matches_scanned": scanned,
                   "credits_remaining": quota["remaining"], "arbs": arbs}, f, ensure_ascii=False, indent=1)
    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(render(arbs, scanned, sports, now))
    print(f"{scanned} mac tarandi, {len(arbs)} arbitraj bulundu. Kalan kredi: {quota['remaining']}")
    for a in arbs:
        print(f"  +{a['profit_pct']}%  {a['match']}  ({a['league']})")
        for l in a["legs"]:
            print(f"      {l['outcome']}: {l['odd']} @ {l['bookmaker']} -> yatir {l['stake']}")


if __name__ == "__main__":
    main()
