"""KBO 2026 시즌 수집기 v3 — SSG 랜더스 대시보드 (https://minyunpapa.github.io/kbo-dashboard).

v3: 리그 전체 경기 박스스코어 증분 캐시 → 리그 선수 집계·세이버메트릭스·리더보드 + 1군 엔트리.

사용:
    python3 collect.py          # data.js (+ records_cache.json 증분 갱신)

데이터 소스 (모두 무인증 공개 API, Naver Sports):
    - 캘린더  /schedule/calendar?categoryId=kbo&date=YYYY-MM-15  (해당 월 전체)
    - 요약    /schedule/games/{gid}
    - 상세    /schedule/games/{gid}/record   (라인스코어·박스스코어·승패세·주요기록)
    - 프리뷰  /schedule/games/{gid}/preview  (선발 예고·라인업·1군 엔트리·상대전적)

설계 노트:
    - records_cache.json: 종료 경기의 트림된 박스스코어를 gid 키로 영구 캐시 (재실행 시 신규만 fetch)
    - 선수 키 = playerCode (동명이인 안전). 2루타/3루타는 etcRecords "이름(N회)" 파싱으로 집계
      → 같은 경기 양 팀에 동명이인이 있으면 해당 건은 건너뜀 (로그)
    - 세이버: OBP·OPS·OPS+·wOBA는 사구(HBP)·희생플라이가 공개 API에 없어 **약식**(주석 표기).
      FIP 상수는 리그 투수 합계에서 정확 산출: C = lgERA - (13·HR + 3·BB - 2·K)/IP
    - 투수 시즌 승패/세이브/홀드 = 경기별 wls(승/패/세/홀) 집계 (boxscore의 seasonWin은 항상 0 — 함정)
    - 팀코드 비교는 반드시 정확 일치 ("SSKT"에 'SK' substring 포함 — 함정)
    - 1군 엔트리 = 가장 가까운 경기 프리뷰의 fullLineUp + batterCandidate + pitcherBullpen
"""
from __future__ import annotations
import json
import re
import sys
import time
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
CACHE_PATH = OUT_DIR / "records_cache.json"

BAT_KEEP = ("playerCode", "batOrder", "pos", "name", "ab", "run", "hit",
            "rbi", "bb", "kk", "sb", "hr", "hra")
PIT_KEEP = ("pcode", "name", "inn", "r", "er", "hit", "bb", "kk", "hr",
            "era", "wls", "bf", "ab")


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def fetch_retry(url: str, attempts: int = 3) -> dict:
    """일시 오류로 경기가 조용히 누락되지 않도록 모든 수집 fetch는 재시도 경유."""
    for i in range(attempts):
        try:
            return fetch(url)
        except Exception:
            if i == attempts - 1:
                raise
            time.sleep(1.2 * (i + 1))


# ────────────────────────── 수집 ──────────────────────────

def month_game_ids(month: int) -> list[str]:
    url = f"{API}/schedule/calendar?categoryId=kbo&date={YEAR}-{month:02d}-15"
    try:
        data = fetch_retry(url)
    except Exception as e:
        print(f"  ! month {month} calendar failed: {e}")
        return []
    return [g for d in data.get("result", {}).get("dates", [])
            for g in d.get("gameIds", []) if KBO_ID.match(g)]


def game_summary(gid: str) -> dict | None:
    try:
        g = fetch_retry(f"{API}/schedule/games/{gid}")["result"]["game"]
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


def fetch_record(gid: str) -> dict | None:
    """종료 경기 박스스코어 (양 팀 트림본) — 캐시 대상."""
    try:
        rec = fetch_retry(f"{API}/schedule/games/{gid}/record")["result"]["recordData"]
    except Exception:
        return None
    sb = rec.get("scoreBoard") or {}
    out = {
        "line": {"inn": sb.get("inn"), "rheb": sb.get("rheb")},
        "wls": [{k: p.get(k) for k in ("name", "wls", "w", "l", "s")}
                for p in rec.get("pitchingResult") or []],
        "etc": [{"how": e.get("how"), "result": e.get("result")}
                for e in (rec.get("etcRecords") or [])],
    }
    for side in ("home", "away"):
        out[f"bat_{side}"] = [{k: p.get(k) for k in BAT_KEEP}
                              for p in (rec.get("battersBoxscore") or {}).get(side, [])]
        out[f"pit_{side}"] = [{k: p.get(k) for k in PIT_KEEP}
                              for p in (rec.get("pitchersBoxscore") or {}).get(side, [])]
    return out


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
    try:
        pv = fetch_retry(f"{API}/schedule/games/{g['gid']}/preview")["result"]["previewData"]
    except Exception:
        return None
    my_side = "home" if g["hc"] == SSG else "away"
    op_side = "away" if my_side == "home" else "home"

    def lineup(side):
        lu = (pv.get(f"{side}TeamLineUp") or {}).get("fullLineUp") or []
        return [{"pos": p.get("positionName"), "name": p.get("playerName"),
                 "bats": p.get("batsThrows")} for p in lu]

    my_team_lu = pv.get(f"{my_side}TeamLineUp") or {}
    vs = pv.get("seasonVsResult") or {}
    if vs.get("aCode") == SSG:
        vs_str = f"{vs.get('aw', 0)}승{('%d무' % vs['ad']) if vs.get('ad') else ''}{vs.get('al', 0)}패"
    elif vs.get("hCode") == SSG:
        vs_str = f"{vs.get('hw', 0)}승{('%d무' % vs['hd']) if vs.get('hd') else ''}{vs.get('hl', 0)}패"
    else:
        vs_str = None
    my_lu = lineup(my_side)
    return {
        "my_starter": starter_brief(pv.get(f"{my_side}Starter")),
        "opp_starter": starter_brief(pv.get(f"{op_side}Starter")),
        "my_lineup": my_lu, "opp_lineup": lineup(op_side),
        "lineup_announced": len(my_lu) >= 9,
        "vs_season": vs_str,
        "entry": {   # SSG 1군 엔트리 (로스터)
            "candidates": [{"name": p.get("playerName"), "pos": p.get("position")}
                           for p in my_team_lu.get("batterCandidate") or []],
            "bullpen": [{"name": p.get("playerName"), "hand": p.get("hitType")}
                        for p in my_team_lu.get("pitcherBullpen") or []],
        },
    }


# ────────────────────────── 리그/순위 가공 ──────────────────────────

def official_standings(games):
    by_date: dict[str, list[dict]] = {}
    for g in games:
        by_date.setdefault(g["date"], []).append(g)
    for d in sorted(by_date, reverse=True):
        day = [g for g in by_date[d] if not g.get("cancel")]
        if day and all(g["status"] == "RESULT" for g in day):
            table = {}
            for g in day:
                try:
                    rec = fetch(f"{API}/schedule/games/{g['gid']}/record")["result"]["recordData"]
                except Exception:
                    continue
                for k in ("homeStandings", "awayStandings"):
                    st = rec.get(k)
                    if st and st.get("name"):
                        table[st["name"]] = st
            if len(table) >= 8:
                return d, table
    return None, {}


def find_opening(games, ref_date, official):
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


def result_for(team, g):
    if g["hs"] == g["as"]:
        return "D"
    return "W" if ((g["hs"] > g["as"]) == (g["home"] == team)) else "L"


def compute_league(games, opening, official):
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
            "tavg": official.get(t, {}).get("hra"),
            "tera": official.get(t, {}).get("era"),
            "w": s["w"], "l": s["l"], "d": s["d"], "pct": round(pct(s), 3),
            "gb": round(((leader["w"] - s["w"]) + (s["l"] - leader["l"])) / 2, 1),
            "streak": streak(s["results"]),
            "last10": f"{last10.count('W')}승{last10.count('D')}무{last10.count('L')}패",
            "home": f"{s['hw']}-{s['hd']}-{s['hl']}",
            "away": f"{s['aw']}-{s['ad']}-{s['al']}",
            "rdiff": s["rf"] - s["ra"],
        })
    standings.sort(key=lambda x: x["rank"])

    fin_by_date: dict[str, list[dict]] = {}
    for g in finals:
        fin_by_date.setdefault(g["date"], []).append(g)
    cum = {t: {"w": 0, "l": 0} for t in teams}
    rank_history = {"dates": [], "ranks": {t: [] for t in teams}}
    for d in sorted(fin_by_date):
        for g in fin_by_date[d]:
            for team in (g["home"], g["away"]):
                r = result_for(team, g)
                if r != "D":
                    cum[team]["w" if r == "W" else "l"] += 1

        def cpct(t):
            c = cum[t]
            return c["w"] / (c["w"] + c["l"]) if (c["w"] + c["l"]) else 0.0
        rank_history["dates"].append(d)
        for i, t in enumerate(sorted(teams, key=lambda t: (-cpct(t), -cum[t]["w"], t)), 1):
            rank_history["ranks"][t].append(i)
    return {"finals": finals, "standings": standings,
            "rank_history": rank_history, "stats": stats}


# ────────────────────────── 선수 집계 (리그 전체) ──────────────────────────

def outs_from_inn(inn) -> int:
    if not inn:
        return 0
    inn = str(inn).strip()
    extra = 1 if "⅓" in inn else 2 if "⅔" in inn else 0
    whole = re.sub(r"[^\d]", " ", inn).split()
    return (int(whole[0]) if whole else 0) * 3 + extra


def inn_display(outs: int) -> str:
    return f"{outs // 3}{['', ' ⅓', ' ⅔'][outs % 3]}"


NAME_INN = re.compile(r"([가-힣A-Za-z·\-]+)\((\d+)회\)")


def parse_xbh(etc: list[dict], how: str) -> list[str]:
    """etcRecords에서 '2루타'/'3루타' 선수명 목록 (등장 횟수만큼 반복)."""
    for e in etc or []:
        if e.get("how") == how:
            return [m[0] for m in NAME_INN.findall(e.get("result") or "")]
    return []


def aggregate_players(finals, cache):
    """리그 전체 타자/투수 집계 (전 경기 게임로그 포함). key=playerCode."""
    bat: dict[str, dict] = {}
    pit: dict[str, dict] = {}
    ambiguous = 0
    for g in finals:
        det = cache.get(g["gid"])
        if not det:
            continue
        name_to_code: dict[str, set] = {}
        for side, team in (("home", g["home"]), ("away", g["away"])):
            opp = g["away"] if side == "home" else g["home"]
            ha = "H" if side == "home" else "A"
            for p in det.get(f"bat_{side}") or []:
                code, nm = p.get("playerCode"), p.get("name")
                if not code or not nm:
                    continue
                name_to_code.setdefault(nm, set()).add(code)
                b = bat.setdefault(code, {
                    "name": nm, "team": team, "g": 0, "ab": 0, "hit": 0,
                    "hr": 0, "rbi": 0, "bb": 0, "kk": 0, "run": 0, "sb": 0,
                    "d2": 0, "d3": 0, "pos": None, "avg": None, "log": []})
                b["g"] += 1
                b["team"] = team
                for k in ("ab", "hit", "hr", "rbi", "bb", "kk", "run", "sb"):
                    b[k] += p.get(k) or 0
                b["pos"] = p.get("pos") or b["pos"]
                b["avg"] = p.get("hra") or b["avg"]
                b["log"].append({
                    "d": g["date"], "o": opp, "ha": ha,
                    "ab": p.get("ab") or 0, "h": p.get("hit") or 0,
                    "hr": p.get("hr") or 0, "bi": p.get("rbi") or 0,
                    "bb": p.get("bb") or 0, "so": p.get("kk") or 0,
                    "r": p.get("run") or 0, "sb": p.get("sb") or 0})
            for idx, p in enumerate(det.get(f"pit_{side}") or []):
                code, nm = p.get("pcode"), p.get("name")
                if not code or not nm:
                    continue
                t = pit.setdefault(code, {
                    "name": nm, "team": team, "g": 0, "gs": 0, "qs": 0,
                    "outs": 0, "hit": 0, "bb": 0, "kk": 0, "er": 0, "r": 0,
                    "hr": 0, "bf": 0, "ab": 0, "w": 0, "l": 0, "sv": 0,
                    "hld": 0, "era": None, "log": []})
                t["g"] += 1
                t["team"] = team
                outs = outs_from_inn(p.get("inn"))
                t["outs"] += outs
                gs = idx == 0
                t["gs"] += gs
                t["qs"] += gs and outs >= 18 and (p.get("er") or 0) <= 3
                for k in ("hit", "bb", "kk", "er", "r", "hr", "bf", "ab"):
                    t[k] += p.get(k) or 0
                t["era"] = p.get("era") or t["era"]
                wls = p.get("wls") or ""
                t["w"] += wls == "승"
                t["l"] += wls == "패"
                t["sv"] += wls == "세"
                t["hld"] += wls == "홀"
                t["log"].append({
                    "d": g["date"], "o": opp, "ha": ha, "gs": int(gs),
                    "ip": p.get("inn"), "out": outs,
                    "h": p.get("hit") or 0, "r": p.get("r") or 0,
                    "er": p.get("er") or 0, "bb": p.get("bb") or 0,
                    "so": p.get("kk") or 0, "wls": wls})
        # 2루타/3루타 — 이름 → playerCode (경기 내 유일할 때만)
        for how, key in (("2루타", "d2"), ("3루타", "d3")):
            for nm in parse_xbh(det.get("etc"), how):
                codes = name_to_code.get(nm) or set()
                if len(codes) == 1:
                    bat[next(iter(codes))][key] += 1
                else:
                    ambiguous += 1
    if ambiguous:
        print(f"      ! 동명이인 등으로 건너뛴 장타 집계 {ambiguous}건")
    return bat, pit


# wOBA 선형가중치 (MLB 표준 계열 고정값 — 리그 OBP에 스케일링해 사용)
W_BB, W_1B, W_2B, W_3B, W_HR = 0.69, 0.88, 1.25, 1.59, 2.05
WOBA_SCALE = 1.20


def sabermetrics(bat, pit, team_games):
    """약식 세이버 (HBP·희생타 미포함 — 주석 필수) + wOBA/wRC+ + 리그 FIP 상수."""
    lg = {"ab": 0, "hit": 0, "bb": 0, "hr": 0, "d2": 0, "d3": 0, "kk": 0, "run": 0}
    for b in bat.values():
        for k in lg:
            lg[k] += b[k]
    lg_tb = lg["hit"] + lg["d2"] + 2 * lg["d3"] + 3 * lg["hr"]
    lg_pa = lg["ab"] + lg["bb"]
    lg_obp = (lg["hit"] + lg["bb"]) / lg_pa
    lg_slg = lg_tb / lg["ab"]
    lg_rpa = lg["run"] / lg_pa            # 리그 득점/타석 (득점 합계는 정확)

    def woba_raw(h, bb, d2, d3, hr, pa):
        s1 = h - d2 - d3 - hr
        return (W_BB * bb + W_1B * s1 + W_2B * d2 + W_3B * d3 + W_HR * hr) / pa

    lg_woba_raw = woba_raw(lg["hit"], lg["bb"], lg["d2"], lg["d3"], lg["hr"], lg_pa)
    woba_k = lg_obp / lg_woba_raw          # 관례: 리그 wOBA = 리그 OBP
    lg_woba = lg_obp

    p_outs = sum(t["outs"] for t in pit.values())
    p_ip = p_outs / 3
    lg_era = sum(t["er"] for t in pit.values()) * 9 / p_ip
    fip_c = lg_era - (13 * sum(t["hr"] for t in pit.values())
                      + 3 * sum(t["bb"] for t in pit.values())
                      - 2 * sum(t["kk"] for t in pit.values())) / p_ip

    for b in bat.values():
        ab, h, bb = b["ab"], b["hit"], b["bb"]
        pa = ab + bb
        tb = h + b["d2"] + 2 * b["d3"] + 3 * b["hr"]
        b["tb"] = tb
        b["s1"] = h - b["d2"] - b["d3"] - b["hr"]
        if not b["avg"]:
            b["avg"] = f"{h / ab:.3f}".lstrip("0") if ab else "-"
        b["obp"] = round((h + bb) / pa, 3) if pa else None
        b["slg"] = round(tb / ab, 3) if ab else None
        b["ops"] = round(b["obp"] + b["slg"], 3) if pa and ab else None
        b["iso"] = round(b["slg"] - (h / ab), 3) if ab else None
        b["bbp"] = round(bb / pa * 100, 1) if pa else None
        b["kp"] = round(b["kk"] / pa * 100, 1) if pa else None
        babip_den = ab - b["kk"] - b["hr"]
        b["babip"] = round((h - b["hr"]) / babip_den, 3) if babip_den > 0 else None
        b["opsp"] = (round(100 * (b["obp"] / lg_obp + b["slg"] / lg_slg - 1))
                     if pa and ab else None)
        if pa:
            woba = woba_raw(h, bb, b["d2"], b["d3"], b["hr"], pa) * woba_k
            b["woba"] = round(woba, 3)
            wraa = (woba - lg_woba) / WOBA_SCALE * pa
            b["wrcp"] = round(100 * ((wraa / pa + lg_rpa) / lg_rpa))
        else:
            b["woba"] = b["wrcp"] = None
        b["pa"] = pa
        b["qualified"] = pa >= team_games * 2.8   # 약식 PA(사구 미포함) 보정 계수

    for t in pit.values():
        ip = t["outs"] / 3
        t["ip"] = inn_display(t["outs"])
        if not t["era"]:
            t["era"] = f"{t['er'] * 9 / ip:.2f}" if ip else "-"
        t["whip"] = round((t["hit"] + t["bb"]) / ip, 2) if ip else None
        t["fip"] = (round((13 * t["hr"] + 3 * t["bb"] - 2 * t["kk"]) / ip + fip_c, 2)
                    if ip else None)
        t["k9"] = round(t["kk"] * 9 / ip, 1) if ip else None
        t["bb9"] = round(t["bb"] * 9 / ip, 1) if ip else None
        t["hr9"] = round(t["hr"] * 9 / ip, 2) if ip else None
        t["kp"] = round(t["kk"] / t["bf"] * 100, 1) if t["bf"] else None
        t["bbp"] = round(t["bb"] / t["bf"] * 100, 1) if t["bf"] else None
        t["oavg"] = (f"{t['hit'] / t['ab']:.3f}".lstrip("0") if t["ab"] else None)
        t["qualified"] = t["outs"] >= team_games * 3   # 규정이닝(팀경기×1)

    return {"obp": round(lg_obp, 3), "slg": round(lg_slg, 3),
            "era": round(lg_era, 2), "fip_c": round(fip_c, 2),
            "woba": round(lg_woba, 3), "rpa": round(lg_rpa, 4),
            "woba_scale": WOBA_SCALE}


def team_detail(finals, cache, bat, pit, official):
    """팀별 세부 지표 — 타격/투수/수비 + 상대전적 + 월별."""
    teams: dict[str, dict] = {}

    def T(name):
        return teams.setdefault(name, {
            "bat": {k: 0 for k in ("ab", "hit", "bb", "kk", "hr", "d2", "d3",
                                   "run", "sb")},
            "pit": {k: 0 for k in ("outs", "hit", "bb", "kk", "er", "r", "hr",
                                   "sv", "ab")},
            "err": 0, "vs": {}, "monthly": {}})

    for b in bat.values():
        d = T(b["team"])["bat"]
        for k in d:
            d[k] += b[k]
    for t in pit.values():
        d = T(t["team"])["pit"]
        for k in d:
            d[k] += t[k]
    for g in finals:
        det = cache.get(g["gid"])
        rheb = ((det or {}).get("line") or {}).get("rheb") or {}
        for side, team in (("home", g["home"]), ("away", g["away"])):
            e = (rheb.get(side) or {}).get("e")
            T(team)["err"] += e or 0
            opp = g["away"] if side == "home" else g["home"]
            r = result_for(team, g)
            key = {"W": "w", "L": "l", "D": "d"}[r]
            tv = T(team)["vs"].setdefault(opp, {"w": 0, "l": 0, "d": 0})
            tv[key] += 1
            mon = f"{int(g['date'][5:7])}월"
            tm = T(team)["monthly"].setdefault(mon, {"w": 0, "l": 0, "d": 0})
            tm[key] += 1

    out = {}
    for name, d in teams.items():
        b, p = d["bat"], d["pit"]
        pa = b["ab"] + b["bb"]
        tb = b["hit"] + b["d2"] + 2 * b["d3"] + 3 * b["hr"]
        ip = p["outs"] / 3
        off = official.get(name, {})
        out[name] = {
            "bat": {
                "avg": off.get("hra") or (round(b["hit"] / b["ab"], 3) if b["ab"] else None),
                "obp": round((b["hit"] + b["bb"]) / pa, 3) if pa else None,
                "slg": round(tb / b["ab"], 3) if b["ab"] else None,
                "ops": round((b["hit"] + b["bb"]) / pa + tb / b["ab"], 3) if pa and b["ab"] else None,
                "run": b["run"], "hr": b["hr"], "sb": b["sb"],
                "bb": b["bb"], "so": b["kk"], "d2": b["d2"], "d3": b["d3"],
            },
            "pit": {
                "era": off.get("era") or (round(p["er"] * 9 / ip, 2) if ip else None),
                "fip": None,   # 아래에서 채움 (리그 상수 필요해 main에서 주입해도 되지만 약식으로 계산)
                "whip": round((p["hit"] + p["bb"]) / ip, 2) if ip else None,
                "so": p["kk"], "bb": p["bb"], "hr": p["hr"],
                "oavg": round(p["hit"] / p["ab"], 3) if p["ab"] else None,
                "sv": p["sv"], "ip_outs": p["outs"], "er": p["er"], "r": p["r"],
            },
            "err": d["err"], "vs": d["vs"], "monthly": d["monthly"],
        }
    return out


def leaderboards(bat, pit):
    """리그 리더보드 — 카테고리별 TOP 10."""
    bq = [b for b in bat.values() if b["qualified"]]
    pq = [t for t in pit.values() if t["qualified"]]
    pall = list(pit.values())

    def fnum(v):
        try:
            return float(str(v))
        except (TypeError, ValueError):
            return 0.0

    def top(rows, key, n=10, asc=False, fmt=None):
        rows = sorted(rows, key=lambda r: fnum(r.get(key)), reverse=not asc)[:n]
        return [{"name": r["name"], "team": r["team"],
                 "v": fmt(r) if fmt else r.get(key)} for r in rows]

    return {
        "bat": {
            "avg":  top(bq, "avg", fmt=lambda r: str(r["avg"])),
            "hr":   top(list(bat.values()), "hr"),
            "rbi":  top(list(bat.values()), "rbi"),
            "sb":   top(list(bat.values()), "sb"),
            "ops":  top(bq, "ops", fmt=lambda r: f"{r['ops']:.3f}".lstrip("0")),
            "wrcp": top(bq, "wrcp"),
        },
        "pit": {
            "era":  top(pq, "era", asc=True, fmt=lambda r: str(r["era"])),
            "fip":  top(pq, "fip", asc=True, fmt=lambda r: f"{r['fip']:.2f}"),
            "w":    top(pall, "w"),
            "sv":   top(pall, "sv"),
            "hld":  top(pall, "hld"),
            "kk":   top(pall, "kk"),
        },
    }


# ────────────────────────── SSG 뷰 ──────────────────────────

def ssg_views(finals, cache, bat, pit):
    ssg_name = next((g["home"] if g["hc"] == SSG else g["away"]
                     for g in finals if SSG in (g["hc"], g["ac"])), "SSG")
    games_out, vs, monthly = [], {}, {}
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
        det = cache.get(g["gid"])
        side = "home" if is_home else "away"
        games_out.append({
            "gid": g["gid"], "date": g["date"], "opp": opp,
            "ha": "H" if is_home else "A", "us": us, "them": them, "r": r,
            "stadium": g["stadium"], "cum": f"{w}-{l}",
            "detail": {
                "line": det["line"], "wls": det["wls"], "etc": det["etc"],
                "bat": det[f"bat_{side}"], "pit": det[f"pit_{side}"],
            } if det else None,
        })
    ssg_bat = sorted([b for b in bat.values() if b["team"] == ssg_name],
                     key=lambda b: (-b["qualified"], -(b["pa"] or 0)))
    ssg_pit = sorted([t for t in pit.values() if t["team"] == ssg_name],
                     key=lambda t: (-t["qualified"], -t["outs"]))
    return ssg_name, games_out, vs, monthly, ssg_bat, ssg_pit


# ────────────────────────── 메인 ──────────────────────────

def main():
    print(f"[1/7] {YEAR} calendar sweep...")
    seen, all_ids = set(), []
    for m in MONTHS:
        for gid in month_game_ids(m):
            if gid not in seen:
                seen.add(gid)
                all_ids.append(gid)
    print(f"      {len(all_ids)} game ids")

    print("[2/7] game summaries...")
    with ThreadPoolExecutor(max_workers=8) as ex:
        games = [g for g in ex.map(game_summary, all_ids) if g and g["date"]]
    print(f"      {len(games)} games, {sum(g['status'] == 'RESULT' for g in games)} finished")

    print("[3/7] official standings + opening day...")
    ref_date, official = official_standings(games)
    opening = find_opening(games, ref_date, official) if official else f"{YEAR}-03-28"
    print(f"      ref={ref_date} opening={opening}")
    league = compute_league(games, opening, official)

    print("[4/7] records cache (리그 전체 박스스코어, 증분)...")
    cache: dict[str, dict] = {}
    if CACHE_PATH.exists():
        try:
            cache = json.loads(CACHE_PATH.read_text())
        except Exception:
            cache = {}
    missing = [g["gid"] for g in league["finals"] if g["gid"] not in cache]
    if missing:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for gid, rec in zip(missing, ex.map(fetch_record, missing)):
                if rec:
                    cache[gid] = rec
        CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False))
    print(f"      cache {len(cache)} games (+{len(missing)} new)")

    print("[5/7] league player aggregation + sabermetrics...")
    bat, pit = aggregate_players(league["finals"], cache)
    team_games = max((s["w"] + s["l"] + s["d"]) for s in league["standings"])
    lg = sabermetrics(bat, pit, team_games)
    boards = leaderboards(bat, pit)
    teams = team_detail(league["finals"], cache, bat, pit, official)
    for td in teams.values():
        ip = td["pit"]["ip_outs"] / 3
        if ip:
            td["pit"]["fip"] = round(
                (13 * td["pit"]["hr"] + 3 * td["pit"]["bb"]
                 - 2 * td["pit"]["so"]) / ip + lg["fip_c"], 2)
    print(f"      batters {len(bat)}, pitchers {len(pit)}, "
          f"lgOBP*{lg['obp']} lgSLG{lg['slg']} FIP상수 {lg['fip_c']} "
          f"lgR/PA {lg['rpa']}")

    print("[6/7] upcoming previews (선발/라인업/엔트리)...")
    today = datetime.now(KST).date().isoformat()
    upcoming = sorted([g for g in games if g["status"] in ("READY", "BEFORE")
                       and SSG in (g["hc"], g["ac"]) and g["date"] >= today],
                      key=lambda g: g["dt"] or g["date"])[:3]
    nexts = []
    roster_cands = []   # (date, roster_dict) — 최신 비어있지 않은 엔트리 채택
    for g in upcoming:
        pv = game_preview(g) or {}
        entry = pv.pop("entry", None)
        nexts.append({
            "date": g["date"], "time": (g["dt"] or "")[11:16],
            "opp": g["away"] if g["hc"] == SSG else g["home"],
            "ha": "H" if g["hc"] == SSG else "A", "stadium": g["stadium"], **pv,
        })
        if entry and (entry["candidates"] or entry["bullpen"]):
            roster_cands.append((g["date"], {
                "date": g["date"],
                "lineup": [p["name"] for p in pv.get("my_lineup") or []],
                **entry}))
        st = (pv.get("my_starter") or {}).get("name") or "미정"
        print(f"      {g['date']} vs {nexts[-1]['opp']}: 선발 {st} / "
              f"라인업{'O' if pv.get('lineup_announced') else 'X'} / "
              f"엔트리 {len((entry or {}).get('bullpen', []))}투수")

    # 최근 종료 경기(오늘 포함)의 프리뷰에서도 엔트리 수집 — 경기 후에도 유지됨
    recent_ssg = [g for g in league["finals"] if SSG in (g["hc"], g["ac"])][-2:]
    for g in reversed(recent_ssg):
        pv = game_preview(g) or {}
        entry = pv.get("entry")
        if entry and (entry["candidates"] or entry["bullpen"]):
            roster_cands.append((g["date"], {
                "date": g["date"],
                "lineup": [p["name"] for p in pv.get("my_lineup") or []],
                **entry}))
    roster = max(roster_cands, key=lambda x: x[0])[1] if roster_cands else None
    roster_path = OUT_DIR / "roster.json"
    if roster:
        roster["stale"] = False
        roster_path.write_text(json.dumps(roster, ensure_ascii=False))
    elif roster_path.exists():   # 폴백: 마지막으로 확인된 엔트리
        try:
            roster = json.loads(roster_path.read_text())
            roster["stale"] = True
        except Exception:
            roster = None
    print(f"      roster: {roster['date'] if roster else 'X'}"
          f"{' (stale)' if roster and roster.get('stale') else ''}")

    live = [{"date": g["date"], "home": g["home"], "away": g["away"],
             "hs": g["hs"], "as": g["as"], "info": g["status_info"],
             "stadium": g["stadium"]} for g in games if g["status"] == "LIVE"]

    print("[7/7] SSG views + output...")
    ssg_name, ssg_games, vs, monthly, ssg_bat, ssg_pit = ssg_views(
        league["finals"], cache, bat, pit)
    me = next((s for s in league["standings"] if s["team"] == ssg_name), {})
    data = {
        "updated": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
        "season": YEAR, "opening": opening, "ssg_team": ssg_name,
        "team_games": team_games,
        "standings": league["standings"],
        "rank_history": league["rank_history"],
        "live": live, "next": nexts, "roster": roster,
        "league": {"means": lg, "boards": boards},
        "teams": teams,
        "ssg": {
            "games": ssg_games, "vs": vs, "monthly": monthly,
            "batters": ssg_bat, "pitchers": ssg_pit,
            "rf": league["stats"].get(ssg_name, {}).get("rf", 0),
            "ra": league["stats"].get(ssg_name, {}).get("ra", 0),
        },
    }
    (OUT_DIR / "season.json").write_text(
        json.dumps({"games": games, **data}, ensure_ascii=False))
    (OUT_DIR / "data.js").write_text(
        "window.KBO_SEASON = " + json.dumps(data, ensure_ascii=False) + ";\n")
    size = (OUT_DIR / "data.js").stat().st_size // 1024
    print(f"DONE — {ssg_name} rank {me.get('rank')}, roster={'O' if roster else 'X'}, "
          f"data.js {size}KB")


if __name__ == "__main__":
    sys.exit(main())
