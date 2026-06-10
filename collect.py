"""KBO 2026 시즌 수집기 v2 — SSG 랜더스 대시보드 (https://minyunpapa.github.io/kbo-dashboard).

Naver Sports API로 시즌 전 경기 + SSG 경기 상세(박스스코어) + 선수 시즌 집계 +
다음 경기 선발 예고/라인업을 수집해 정적 data.js를 생성한다.

사용:
    python3 collect.py          # data.js + season.json 생성 (약 1~2분)

데이터 소스 (모두 무인증 공개 API):
    - 캘린더: /schedule/calendar?categoryId=kbo&date=YYYY-MM-15 → 해당 월 전체
    - 경기 요약: /schedule/games/{gid}
    - 경기 상세: /schedule/games/{gid}/record  (라인스코어·박스스코어·승패세·주요기록)
    - 경기 프리뷰: /schedule/games/{gid}/preview (선발 예고·라인업·시즌 상대전적)

설계 노트:
    - KBO 경기 ID 패턴: ^\\d{8}[A-Z]{2}[A-Z]{2}\\d{4,5}$ (타 종목 혼재 응답에서 필터)
    - SSG 팀 코드 "SK" — 반드시 hc/ac 정확 비교 (substring 매칭 금지: "SSKT"에 'SK' 포함됨)
    - 시범경기 제외: 공식 순위(record standings)와 누적 W/L/D 일치하는 개막일 자동 탐색
    - 선수 시즌 스탯: 모든 SSG 경기 박스스코어 합산 (rate 스탯은 최신 경기 라인의 공식값)
"""
from __future__ import annotations
import json
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date, timezone, timedelta
from pathlib import Path

KST = timezone(timedelta(hours=9))

API = "https://api-gw.sports.naver.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Accept": "application/json",
    "Referer": "https://sports.news.naver.com/kbaseball/index",
}
KBO_ID = re.compile(r"^\d{8}[A-Z]{2}[A-Z]{2}\d{4,5}$")
SSG = "SK"
YEAR = 2026
MONTHS = range(3, 11)
OUT_DIR = Path(__file__).parent.resolve()


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


# ────────────────────────── 수집 ──────────────────────────

def month_game_ids(month: int) -> list[str]:
    url = f"{API}/schedule/calendar?categoryId=kbo&date={YEAR}-{month:02d}-15"
    try:
        data = fetch(url)
    except Exception as e:
        print(f"  ! month {month} calendar failed: {e}")
        return []
    return [g for d in data.get("result", {}).get("dates", [])
            for g in d.get("gameIds", []) if KBO_ID.match(g)]


def game_summary(gid: str) -> dict | None:
    try:
        g = fetch(f"{API}/schedule/games/{gid}")["result"]["game"]
    except Exception:
        return None
    return {
        "gid": gid, "date": g.get("gameDate"), "dt": g.get("gameDateTime"),
        "status": g.get("statusCode"), "status_info": g.get("statusInfo"),
        "winner": g.get("winner"),
        "home": g.get("homeTeamShortName"), "away": g.get("awayTeamShortName"),
        "hc": g.get("homeTeamCode"), "ac": g.get("awayTeamCode"),
        "hs": g.get("homeTeamScore"), "as": g.get("awayTeamScore"),
        "stadium": g.get("stadium"), "suspended": g.get("suspended"),
        "cancel": g.get("cancel"),
    }


def outs_from_inn(inn: str) -> int:
    """'6 ⅔' / '0 ⅓' / '7' → 아웃카운트."""
    if not inn:
        return 0
    inn = str(inn).strip()
    extra = 0
    if "⅓" in inn:
        extra = 1
    elif "⅔" in inn:
        extra = 2
    whole = re.sub(r"[^\d]", " ", inn).split()
    return (int(whole[0]) if whole else 0) * 3 + extra


def inn_display(outs: int) -> str:
    return f"{outs // 3}{['', ' ⅓', ' ⅔'][outs % 3]}"


BAT_KEEP = ("batOrder", "pos", "name", "ab", "run", "hit", "rbi",
            "bb", "kk", "sb", "hr", "hra")
PIT_KEEP = ("name", "inn", "r", "er", "hit", "bb", "kk", "hr", "era", "wls")


def game_record(g: dict) -> dict | None:
    """SSG 경기 상세 — 라인스코어·승패세·주요기록·SSG 박스스코어."""
    try:
        rec = fetch(f"{API}/schedule/games/{g['gid']}/record")["result"]["recordData"]
    except Exception:
        return None
    side = "home" if g["hc"] == SSG else "away"
    sb = rec.get("scoreBoard") or {}
    bat = [{k: p.get(k) for k in BAT_KEEP}
           for p in (rec.get("battersBoxscore") or {}).get(side, [])]
    pit = [{k: p.get(k) for k in PIT_KEEP}
           for p in (rec.get("pitchersBoxscore") or {}).get(side, [])]
    return {
        "gid": g["gid"],
        "line": {"inn": sb.get("inn"), "rheb": sb.get("rheb")},
        "wls": [{k: p.get(k) for k in ("name", "wls", "w", "l", "s")}
                for p in rec.get("pitchingResult") or []],
        "etc": [{"how": e.get("how"), "result": e.get("result")}
                for e in (rec.get("etcRecords") or [])[:8]],
        "bat": bat, "pit": pit,
    }


def starter_brief(st: dict | None) -> dict | None:
    if not st or not st.get("playerInfo"):
        return None
    info, cs = st["playerInfo"], st.get("currentSeasonStats") or {}
    vs = st.get("currentSeasonStatsOnOpponents") or {}
    return {
        "name": info.get("name"), "hand": info.get("hitType"),
        "backnum": info.get("backnum"),
        "era": cs.get("era"), "w": cs.get("w"), "l": cs.get("l"),
        "g": cs.get("gameCount"), "inn": cs.get("inn2") or cs.get("inn"),
        "whip": cs.get("whip"), "kk": cs.get("kk"),
        "vs_era": vs.get("era"), "vs_w": vs.get("w"), "vs_l": vs.get("l"),
        "vs_g": vs.get("gameCount"),
    }


def game_preview(g: dict) -> dict | None:
    """다음 경기 선발 예고 + 라인업."""
    try:
        pv = fetch(f"{API}/schedule/games/{g['gid']}/preview")["result"]["previewData"]
    except Exception:
        return None
    my_side = "home" if g["hc"] == SSG else "away"
    op_side = "away" if my_side == "home" else "home"

    def lineup(side: str) -> list[dict]:
        lu = (pv.get(f"{side}TeamLineUp") or {}).get("fullLineUp") or []
        return [{"pos": p.get("positionName"), "name": p.get("playerName"),
                 "bats": p.get("batsThrows")} for p in lu]

    vs = pv.get("seasonVsResult") or {}
    if vs.get("aCode") == SSG:
        vs_str = f"{vs.get('aw', 0)}승{('%d무' % vs['ad']) if vs.get('ad') else ''}{vs.get('al', 0)}패"
    elif vs.get("hCode") == SSG:
        vs_str = f"{vs.get('hw', 0)}승{('%d무' % vs['hd']) if vs.get('hd') else ''}{vs.get('hl', 0)}패"
    else:
        vs_str = None
    my_lu, op_lu = lineup(my_side), lineup(op_side)
    return {
        "my_starter": starter_brief(pv.get(f"{my_side}Starter")),
        "opp_starter": starter_brief(pv.get(f"{op_side}Starter")),
        "my_lineup": my_lu, "opp_lineup": op_lu,
        "lineup_announced": len(my_lu) >= 9,
        "vs_season": vs_str,
    }


# ────────────────────────── 가공 ──────────────────────────

def official_standings(games: list[dict]) -> tuple[str | None, dict]:
    by_date: dict[str, list[dict]] = {}
    for g in games:
        by_date.setdefault(g["date"], []).append(g)
    for d in sorted(by_date, reverse=True):
        day = [g for g in by_date[d] if not g.get("cancel")]
        if day and all(g["status"] == "RESULT" for g in day):
            table: dict[str, dict] = {}
            for g in day:
                try:
                    rec = fetch(f"{API}/schedule/games/{g['gid']}/record")["result"]["recordData"]
                except Exception:
                    continue
                for s_key in ("homeStandings", "awayStandings"):
                    st = rec.get(s_key)
                    if st and st.get("name"):
                        table[st["name"]] = st
            if len(table) >= 8:
                return d, table
    return None, {}


def find_opening(games: list[dict], ref_date: str, official: dict) -> str:
    finals = [g for g in games if g["status"] == "RESULT" and not g.get("cancel")
              and g["date"] <= ref_date]
    dates = sorted({g["date"] for g in finals})
    target = {n: (s.get("w", 0) + s.get("l", 0) + s.get("d", 0))
              for n, s in official.items()}
    for cut in dates:
        played: dict[str, int] = {}
        for g in finals:
            if g["date"] >= cut:
                played[g["home"]] = played.get(g["home"], 0) + 1
                played[g["away"]] = played.get(g["away"], 0) + 1
        if played and all(played.get(n, -1) == c for n, c in target.items()):
            return cut
    gaps = [(d2, (date.fromisoformat(d2) - date.fromisoformat(d1)).days)
            for d1, d2 in zip(dates, dates[1:])]
    big = [d for d, n in gaps if n >= 5]
    return big[0] if big else (dates[0] if dates else f"{YEAR}-03-28")


def result_for(team: str, g: dict) -> str:
    if g["hs"] == g["as"]:
        return "D"
    return "W" if ((g["hs"] > g["as"]) == (g["home"] == team)) else "L"


def compute_league(games: list[dict], opening: str, official: dict) -> dict:
    finals = sorted([g for g in games if g["status"] == "RESULT"
                     and not g.get("cancel") and g["date"] >= opening],
                    key=lambda g: g["dt"] or g["date"])
    teams = sorted({g["home"] for g in finals} | {g["away"] for g in finals})
    stats = {t: {"w": 0, "l": 0, "d": 0, "rf": 0, "ra": 0,
                 "hw": 0, "hl": 0, "hd": 0, "aw": 0, "al": 0, "ad": 0,
                 "results": []} for t in teams}
    for g in finals:
        for team, at_home in ((g["home"], True), (g["away"], False)):
            s = stats[team]
            r = result_for(team, g)
            key = {"W": "w", "L": "l", "D": "d"}[r]
            s["results"].append(r)
            s[key] += 1
            s[("h" if at_home else "a") + key] += 1
            s["rf"] += (g["hs"] if at_home else g["as"]) or 0
            s["ra"] += (g["as"] if at_home else g["hs"]) or 0

    def pct(s):
        return s["w"] / (s["w"] + s["l"]) if (s["w"] + s["l"]) else 0.0

    def streak(results):
        run = [r for r in results if r != "D"]
        if not run:
            return "-"
        last, n = run[-1], 0
        for r in reversed(run):
            if r != last:
                break
            n += 1
        return f"{n}{'승' if last == 'W' else '패'}"

    ordered = sorted(teams, key=lambda t: (-pct(stats[t]), -stats[t]["w"], t))
    leader = stats[ordered[0]]
    standings = []
    for i, t in enumerate(ordered, 1):
        s = stats[t]
        last10 = s["results"][-10:]
        standings.append({
            "rank": official.get(t, {}).get("rank") or i, "team": t,
            "w": s["w"], "l": s["l"], "d": s["d"],
            "pct": round(pct(s), 3),
            "gb": round(((leader["w"] - s["w"]) + (s["l"] - leader["l"])) / 2, 1),
            "streak": streak(s["results"]),
            "last10": f"{last10.count('W')}승{last10.count('D')}무{last10.count('L')}패",
            "home": f"{s['hw']}-{s['hd']}-{s['hl']}",
            "away": f"{s['aw']}-{s['ad']}-{s['al']}",
            "rdiff": s["rf"] - s["ra"],
        })
    standings.sort(key=lambda x: x["rank"])

    hist_dates = sorted({g["date"] for g in finals})
    fin_by_date: dict[str, list[dict]] = {}
    for g in finals:
        fin_by_date.setdefault(g["date"], []).append(g)
    cum = {t: {"w": 0, "l": 0} for t in teams}
    rank_history = {"dates": [], "ranks": {t: [] for t in teams}}
    for d in hist_dates:
        for g in fin_by_date[d]:
            for team in (g["home"], g["away"]):
                r = result_for(team, g)
                if r != "D":
                    cum[team]["w" if r == "W" else "l"] += 1

        def cpct(t):
            c = cum[t]
            return c["w"] / (c["w"] + c["l"]) if (c["w"] + c["l"]) else 0.0
        day_order = sorted(teams, key=lambda t: (-cpct(t), -cum[t]["w"], t))
        rank_history["dates"].append(d)
        for i, t in enumerate(day_order, 1):
            rank_history["ranks"][t].append(i)
    return {"finals": finals, "standings": standings,
            "rank_history": rank_history, "stats": stats}


def compute_ssg(finals: list[dict], details: dict[str, dict]) -> dict:
    """SSG 게임로그 + 선수 시즌 집계."""
    ssg_name = next((g["home"] if g["hc"] == SSG else g["away"]
                     for g in finals if SSG in (g["hc"], g["ac"])), "SSG")
    games_out, vs, monthly = [], {}, {}
    batters: dict[str, dict] = {}
    pitchers: dict[str, dict] = {}
    w = l = 0
    for g in finals:
        if g["hc"] != SSG and g["ac"] != SSG:
            continue
        is_home = g["hc"] == SSG
        opp = g["away"] if is_home else g["home"]
        us, them = (g["hs"], g["as"]) if is_home else (g["as"], g["hs"])
        r = result_for(ssg_name, g)
        w += r == "W"
        l += r == "L"
        key = {"W": "w", "L": "l", "D": "d"}[r]
        vs.setdefault(opp, {"w": 0, "l": 0, "d": 0})[key] += 1
        monthly.setdefault(f"{int(g['date'][5:7])}월", {"w": 0, "l": 0, "d": 0})[key] += 1
        det = details.get(g["gid"])
        games_out.append({
            "gid": g["gid"], "date": g["date"], "opp": opp,
            "ha": "H" if is_home else "A", "us": us, "them": them, "r": r,
            "stadium": g["stadium"], "cum": f"{w}-{l}",
            "detail": det,
        })
        if not det:
            continue
        # 타자 집계
        for p in det["bat"]:
            if not p.get("name"):
                continue
            b = batters.setdefault(p["name"], {
                "name": p["name"], "g": 0, "ab": 0, "hit": 0, "hr": 0,
                "rbi": 0, "bb": 0, "kk": 0, "run": 0, "sb": 0,
                "pos": None, "avg": None, "recent": []})
            b["g"] += 1
            for k in ("ab", "hit", "hr", "rbi", "bb", "kk", "run", "sb"):
                b[k] += p.get(k) or 0
            b["pos"] = p.get("pos") or b["pos"]
            b["avg"] = p.get("hra") or b["avg"]   # 최신 경기의 공식 시즌 타율
            b["recent"].append({"date": g["date"], "opp": opp,
                                "ab": p.get("ab"), "hit": p.get("hit"),
                                "hr": p.get("hr"), "rbi": p.get("rbi")})
        # 투수 집계
        for p in det["pit"]:
            if not p.get("name"):
                continue
            t = pitchers.setdefault(p["name"], {
                "name": p["name"], "g": 0, "outs": 0, "hit": 0, "bb": 0,
                "kk": 0, "er": 0, "r": 0, "hr": 0, "w": 0, "l": 0,
                "sv": 0, "hld": 0, "era": None, "recent": []})
            t["g"] += 1
            t["outs"] += outs_from_inn(p.get("inn"))
            for k in ("hit", "bb", "kk", "er", "r", "hr"):
                t[k] += p.get(k) or 0
            t["era"] = p.get("era") or t["era"]
            # 승/패/세/홀은 경기별 wls 집계 = 시즌 전체와 일치 (선수의 모든 등판이 SSG 경기)
            wls = p.get("wls") or ""
            t["w"] += wls == "승"
            t["l"] += wls == "패"
            t["sv"] += wls == "세"
            t["hld"] += wls == "홀"
            t["recent"].append({"date": g["date"], "opp": opp,
                                "inn": p.get("inn"), "er": p.get("er"),
                                "kk": p.get("kk"), "wls": wls})

    for b in batters.values():
        b["recent"] = b["recent"][-5:]
        if not b["avg"]:
            b["avg"] = f"{(b['hit'] / b['ab']):.3f}".lstrip("0") if b["ab"] else "-"
    for t in pitchers.values():
        t["recent"] = t["recent"][-5:]
        t["ip"] = inn_display(t["outs"])
        ip = t["outs"] / 3
        t["whip"] = f"{(t['hit'] + t['bb']) / ip:.2f}" if ip else "-"
        if not t["era"]:
            t["era"] = f"{t['er'] * 9 / ip:.2f}" if ip else "-"

    bat_list = sorted(batters.values(),
                      key=lambda b: (-(b["ab"] >= 50),
                                     -float(str(b["avg"]).replace("-", "0") or 0)))
    pit_list = sorted(pitchers.values(),
                      key=lambda t: (-(t["outs"] >= 60),
                                     float(str(t["era"]).replace("-", "99") or 99)))
    return {"name": ssg_name, "games": games_out, "vs": vs,
            "monthly": monthly, "batters": bat_list, "pitchers": pit_list}


# ────────────────────────── 메인 ──────────────────────────

def main():
    print(f"[1/6] {YEAR} calendar sweep...")
    seen, all_ids = set(), []
    for m in MONTHS:
        for gid in month_game_ids(m):
            if gid not in seen:
                seen.add(gid)
                all_ids.append(gid)
    print(f"      {len(all_ids)} game ids")

    print("[2/6] game summaries...")
    with ThreadPoolExecutor(max_workers=8) as ex:
        games = [g for g in ex.map(game_summary, all_ids) if g and g["date"]]
    print(f"      {len(games)} games, {sum(g['status'] == 'RESULT' for g in games)} finished")

    print("[3/6] official standings + opening day...")
    ref_date, official = official_standings(games)
    opening = find_opening(games, ref_date, official) if official else f"{YEAR}-03-28"
    print(f"      ref={ref_date} opening={opening}")

    league = compute_league(games, opening, official)

    print("[4/6] SSG game records (boxscores)...")
    ssg_finals = [g for g in league["finals"] if SSG in (g["hc"], g["ac"])]
    with ThreadPoolExecutor(max_workers=8) as ex:
        recs = list(ex.map(game_record, ssg_finals))
    details = {r["gid"]: r for r in recs if r}
    print(f"      {len(details)}/{len(ssg_finals)} records")

    ssg = compute_ssg(league["finals"], details)

    print("[5/6] upcoming previews (선발/라인업)...")
    today = datetime.now(KST).date().isoformat()   # Actions 러너(UTC)에서도 KST 기준
    upcoming = sorted([g for g in games if g["status"] in ("READY", "BEFORE")
                       and SSG in (g["hc"], g["ac"]) and g["date"] >= today],
                      key=lambda g: g["dt"] or g["date"])[:3]
    nexts = []
    for g in upcoming:
        pv = game_preview(g) or {}
        nexts.append({
            "date": g["date"], "time": (g["dt"] or "")[11:16],
            "opp": g["away"] if g["hc"] == SSG else g["home"],
            "ha": "H" if g["hc"] == SSG else "A", "stadium": g["stadium"],
            **pv,
        })
        lu = "라인업O" if pv.get("lineup_announced") else "라인업X"
        st = (pv.get("my_starter") or {}).get("name") or "미정"
        print(f"      {g['date']} vs {nexts[-1]['opp']}: 선발 {st} / {lu}")

    live = [{"date": g["date"], "home": g["home"], "away": g["away"],
             "hs": g["hs"], "as": g["as"], "info": g["status_info"],
             "stadium": g["stadium"]} for g in games if g["status"] == "LIVE"]

    me = next((s for s in league["standings"] if s["team"] == ssg["name"]), {})
    data = {
        "updated": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
        "season": YEAR, "opening": opening, "ssg_team": ssg["name"],
        "standings": league["standings"],
        "rank_history": league["rank_history"],
        "live": live,
        "next": nexts,
        "ssg": {
            "games": ssg["games"], "vs": ssg["vs"], "monthly": ssg["monthly"],
            "batters": ssg["batters"], "pitchers": ssg["pitchers"],
            "rf": league["stats"].get(ssg["name"], {}).get("rf", 0),
            "ra": league["stats"].get(ssg["name"], {}).get("ra", 0),
        },
    }

    print("[6/6] writing output...")
    (OUT_DIR / "season.json").write_text(
        json.dumps({"games": games, **data}, ensure_ascii=False))
    (OUT_DIR / "data.js").write_text(
        "window.KBO_SEASON = " + json.dumps(data, ensure_ascii=False) + ";\n")
    size = (OUT_DIR / "data.js").stat().st_size // 1024
    print(f"DONE — {ssg['name']} rank {me.get('rank')}, "
          f"{len(ssg['games'])}G, batters {len(ssg['batters'])}, "
          f"pitchers {len(ssg['pitchers'])}, data.js {size}KB")


if __name__ == "__main__":
    sys.exit(main())
