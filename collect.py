"""
은행 취업 허브 - 데이터 수집기

  python collector/collect.py              # 평소 실행 (최근 7일 뉴스 + 채용공고)
  python collector/collect.py --backfill   # 최초 1회: 최근 6개월 뉴스를 주 단위로 채움

환경변수 (모두 선택. 없으면 해당 소스만 건너뜀)
  NAVER_CLIENT_ID / NAVER_CLIENT_SECRET   네이버 검색 API (뉴스)
  SARAMIN_ACCESS_KEY                      사람인 오픈API (채용공고)
  ECOS_API_KEY                            한국은행 ECOS 오픈API (경제지표)

결과물: docs/data/news.json, jobs.json, econ.json, meta.json
뉴스는 매번 기존 파일에 누적 저장되고, 183일이 지난 기사는 자동 삭제된다.
"""
from __future__ import annotations

import argparse
import email.utils
import html
import json
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data"
KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)
KEEP_DAYS = 183
UA = {"User-Agent": "Mozilla/5.0 (bank-career-hub collector)"}

# 제목에 이 단어가 있으면 '채용' 기사로 분류
JOB_WORDS = ["채용", "공채", "신입행원", "모집", "인턴", "채용형", "필기시험", "서류전형", "면접", "합격", "일자리"]

# 제목에 이 단어가 있으면 '경제' 기사로 분류
ECON_WORDS = ["금리", "기준금리", "대출", "예금", "적금", "환율", "물가", "경제", "성장률", "부동산", "주담대",
              "가계부채", "가계대출", "연체", "실적", "순이익", "영업이익", "증시", "코스피", "수출", "금융위",
              "금감원", "한은", "한국은행", "유동성", "건전성", "자본", "배당", "PF", "경기", "인플레", "달러"]

# 은행과 무관한 거시경제 뉴스 (뉴스란 '경제' 카테고리에 함께 표시)
MACRO = {"id": "macro", "name": "경제", "aliases": [],
         "queries": ["기준금리", "한국은행 금리", "원달러 환율", "소비자물가", "가계부채", "경제성장률", "금융시장"]}

# 한국은행 ECOS 지표: (id, 이름, 단위, 통계표, 주기, 항목코드)
#  코드는 ECOS 'Open API > 통계코드검색'에서 확인·변경할 수 있다.
INDICATORS = [
    ("base",   "기준금리",      "%",  "722Y001", "D", "0101000"),
    ("ktb3",   "국고채 3년",    "%",  "817Y002", "D", "010200000"),
    ("cd91",   "CD 91일",       "%",  "817Y002", "D", "010502000"),
    ("usdkrw", "원/달러 환율",  "원", "731Y001", "D", "0000001"),
    ("kospi",  "코스피",        "pt", "802Y001", "D", "0001000"),
    ("cpi",    "소비자물가 상승률", "%", "901Y009", "M", "0"),   # 지수를 받아 전년동월비로 변환
]

session = requests.Session()
session.headers.update(UA)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    return html.unescape(text).strip()


def norm_key(title: str) -> str:
    """중복 판별용 키: 공백·기호 제거"""
    return re.sub(r"[\W_]+", "", title).lower()[:60]


def is_job_news(title: str) -> bool:
    return any(w in title for w in JOB_WORDS)


def is_econ_news(title: str) -> bool:
    return any(w in title for w in ECON_WORDS)


def mentions(bank: dict, text: str) -> bool:
    return not bank["aliases"] or any(a in text for a in bank["aliases"])


# ---------------------------------------------------------------- 뉴스: 구글 뉴스 RSS (키 불필요)
def google_news(bank: dict, extra: str, query: str | None = None) -> list[dict]:
    q = (query or " OR ".join(f'"{a}"' for a in bank["aliases"])) + " " + extra
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q, "hl": "ko", "gl": "KR", "ceid": "KR:ko"})
    try:
        r = session.get(url, timeout=20)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:  # noqa: BLE001
        log(f"  [google] {bank['name']} 실패: {e}")
        return []
    out = []
    for it in root.iter("item"):
        title = clean(it.findtext("title", ""))
        src_el = it.find("source")
        source = clean(src_el.text) if src_el is not None and src_el.text else ""
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3].strip()
        if not mentions(bank, title):  # 제목에 은행명이 있는 기사만
            continue
        try:
            dt = email.utils.parsedate_to_datetime(it.findtext("pubDate", "")).astimezone(KST)
        except Exception:  # noqa: BLE001
            continue
        out.append({"bank": bank["id"], "title": title, "source": source,
                    "link": it.findtext("link", ""), "date": dt.isoformat(), "via": "google"})
    return out


# ---------------------------------------------------------------- 뉴스: 네이버 검색 API (선택)
def naver_news(bank: dict, pages: int = 3) -> list[dict]:
    cid, sec = os.getenv("NAVER_CLIENT_ID"), os.getenv("NAVER_CLIENT_SECRET")
    if not (cid and sec):
        return []
    out = []
    for alias in bank["aliases"] or bank.get("queries", []):
        for p in range(pages):
            try:
                r = session.get("https://openapi.naver.com/v1/search/news.json",
                                headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": sec},
                                params={"query": alias, "display": 100, "start": 1 + p * 100, "sort": "date"},
                                timeout=20)
                r.raise_for_status()
                items = r.json().get("items", [])
            except Exception as e:  # noqa: BLE001
                log(f"  [naver] {alias} 실패: {e}")
                break
            for it in items:
                title = clean(it["title"])
                if not mentions(bank, title + clean(it.get("description", ""))):
                    continue
                dt = email.utils.parsedate_to_datetime(it["pubDate"]).astimezone(KST)
                link = it.get("originallink") or it["link"]
                host = urllib.parse.urlparse(link).netloc.replace("www.", "")
                out.append({"bank": bank["id"], "title": title, "source": host,
                            "link": link, "date": dt.isoformat(), "via": "naver"})
            if len(items) < 100:
                break
            time.sleep(0.1)
    return out


# ---------------------------------------------------------------- 채용공고: 사람인 오픈API (선택)
def saramin_jobs(bank: dict) -> list[dict]:
    key = os.getenv("SARAMIN_ACCESS_KEY")
    if not key:
        return []
    try:
        r = session.get("https://oapi.saramin.co.kr/job-search",
                        headers={"Accept": "application/json"},
                        params={"access-key": key, "keywords": bank["saramin"], "count": 110,
                                "sort": "pd", "fields": "posting-date,expiration-date"},
                        timeout=20)
        r.raise_for_status()
        jobs = r.json().get("jobs", {}).get("job", [])
    except Exception as e:  # noqa: BLE001
        log(f"  [saramin] {bank['name']} 실패: {e}")
        return []
    out = []
    for j in jobs:
        company = j.get("company", {}).get("detail", {}).get("name", "")
        if not mentions(bank, company) and bank["saramin"] not in company:
            continue  # 키워드만 들어간 타사 공고 제외
        pos = j.get("position", {})

        def ts(k):
            v = j.get(k)
            try:
                return datetime.fromtimestamp(int(v), KST).isoformat() if v else None
            except (TypeError, ValueError):
                return None

        out.append({
            "bank": bank["id"],
            "title": clean(pos.get("title", "")),
            "company": company,
            "link": j.get("url", ""),
            "posted": ts("posting-timestamp"),
            "deadline": ts("expiration-timestamp"),
            "close_type": (j.get("close-type") or {}).get("name", ""),
            "career": (pos.get("experience-level") or {}).get("name", ""),
            "job_type": (pos.get("job-type") or {}).get("name", ""),
            "location": clean((pos.get("location") or {}).get("name", "")).replace(",", " · "),
            "via": "saramin",
        })
    return out


# ---------------------------------------------------------------- 경제지표: 한국은행 ECOS (선택)
def ecos_series(key: str, stat: str, cycle: str, item: str, start: str, end: str) -> list[list]:
    url = f"https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/1/10000/{stat}/{cycle}/{start}/{end}/{item}"
    r = session.get(url, timeout=30)
    r.raise_for_status()
    js = r.json()
    if "StatisticSearch" not in js:
        raise RuntimeError(js.get("RESULT", js))
    pts = []
    for row in js["StatisticSearch"]["row"]:
        t, v = row["TIME"], row.get("DATA_VALUE")
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        d = f"{t[:4]}-{t[4:6]}-{t[6:8]}" if len(t) == 8 else f"{t[:4]}-{t[4:6]}-01"
        pts.append([d, v])
    return pts


def economy(old: dict) -> dict:
    key = os.getenv("ECOS_API_KEY")
    if not key:
        return old
    series = dict(old.get("series", {}))
    for sid, name, unit, stat, cycle, item in INDICATORS:
        try:
            if cycle == "D":
                pts = ecos_series(key, stat, "D", item, f"{NOW - timedelta(days=3 * 365 + 10):%Y%m%d}", f"{NOW:%Y%m%d}")
            else:
                pts = ecos_series(key, stat, "M", item, f"{NOW.year - 4}01", f"{NOW:%Y%m}")
                if sid == "cpi":  # 지수 → 전년동월비(%)
                    idx = dict(pts)
                    pts = []
                    for d, v in idx.items():
                        prev = f"{int(d[:4]) - 1}{d[4:]}"
                        if prev in idx:
                            pts.append([d, round((v / idx[prev] - 1) * 100, 2)])
            if pts:
                series[sid] = {"name": name, "unit": unit, "freq": cycle, "points": pts}
                log(f"  [ecos] {name}: {len(pts)}개, 최근 {pts[-1]}")
        except Exception as e:  # noqa: BLE001
            log(f"  [ecos] {name} 실패 (기존 값 유지): {e}")
        time.sleep(0.2)
    return {"updated": NOW.isoformat(), "series": series,
            "order": [i[0] for i in INDICATORS]}


# ---------------------------------------------------------------- 병합·저장
def merge_news(old: list[dict], new: list[dict]) -> list[dict]:
    cutoff = NOW - timedelta(days=KEEP_DAYS)
    seen, merged = set(), []
    # 같은 기사가 겹치면 네이버(언론사 원문 링크) 쪽을 남긴다
    pool = sorted(old + new, key=lambda x: (0 if x.get("via") == "naver" else 1))
    for n in pool:
        try:
            if datetime.fromisoformat(n["date"]) < cutoff:
                continue
        except ValueError:
            continue
        k = (n["bank"], norm_key(n["title"]))
        if k in seen:
            continue
        seen.add(k)
        n["job"] = is_job_news(n["title"])
        n["econ"] = n["bank"] == "macro" or is_econ_news(n["title"])
        merged.append(n)
    merged.sort(key=lambda x: x["date"], reverse=True)
    return merged


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true", help="최근 6개월 뉴스를 주 단위로 채운다 (최초 1회)")
    args = ap.parse_args()

    banks = json.loads((ROOT / "banks.json").read_text(encoding="utf-8"))
    news_path, jobs_path = DATA / "news.json", DATA / "jobs.json"
    old_news = load_json(news_path, [])

    fresh: list[dict] = []
    jobs: list[dict] = []
    for b in banks:
        log(f"· {b['name']}")
        if args.backfill:
            # 구글 뉴스 RSS는 검색 1회당 최대 100건이라 주 단위로 쪼개서 요청
            for w in range(0, 27):
                end = NOW - timedelta(days=7 * w)
                start = end - timedelta(days=7)
                fresh += google_news(b, f"after:{start:%Y-%m-%d} before:{(end + timedelta(days=1)):%Y-%m-%d}")
                time.sleep(0.7)
        else:
            fresh += google_news(b, "when:7d")
            fresh += google_news(b, "채용 when:30d")  # 채용 기사는 따로 한 번 더
        fresh += naver_news(b, pages=10 if args.backfill else 2)
        jobs += saramin_jobs(b)
        time.sleep(0.3)

    # 거시경제 뉴스
    log("· 경제(거시)")
    for q in MACRO["queries"]:
        fresh += google_news(MACRO, "when:30d" if args.backfill else "when:7d", query=f'"{q}"')
        time.sleep(0.3)
    fresh += naver_news(MACRO, pages=1)

    news = merge_news(old_news, fresh)
    save_json(news_path, news)

    # 채용공고: 마감되지 않은 것만. 사람인 키가 없으면 기존 파일 유지
    if os.getenv("SARAMIN_ACCESS_KEY"):
        today = NOW.isoformat()
        jobs = [j for j in jobs if not j["deadline"] or j["deadline"] >= today]
        uniq = {j["link"]: j for j in jobs}
        jobs = sorted(uniq.values(), key=lambda j: j["deadline"] or "9999")
        save_json(jobs_path, jobs)
    elif not jobs_path.exists():
        save_json(jobs_path, [])

    econ = economy(load_json(DATA / "econ.json", {}))
    save_json(DATA / "econ.json", econ)

    meta = {
        "updated": NOW.isoformat(),
        "sources": {
            "google_news": True,
            "naver": bool(os.getenv("NAVER_CLIENT_ID")),
            "saramin": bool(os.getenv("SARAMIN_ACCESS_KEY")),
            "ecos": bool(os.getenv("ECOS_API_KEY")),
        },
        "banks": banks,
    }
    save_json(DATA / "meta.json", meta)
    log(f"완료: 뉴스 {len(news)}건 (신규 수집 {len(fresh)}건), 채용공고 {len(load_json(jobs_path, []))}건")


if __name__ == "__main__":
    main()
