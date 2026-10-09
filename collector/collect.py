"""
은행 취업 허브 - 데이터 수집기

  python collector/collect.py              # 평소 실행 (최근 7일 뉴스 + 채용공고)
  python collector/collect.py --backfill   # 최초 1회: 최근 6개월 뉴스를 주 단위로 채움

환경변수 (모두 선택. 없으면 해당 소스만 건너뜀)
  NAVER_CLIENT_ID / NAVER_CLIENT_SECRET   네이버 검색 API (뉴스)
  SARAMIN_ACCESS_KEY                      사람인 오픈API (채용공고)
  ECOS_API_KEY                            한국은행 ECOS 오픈API (경제지표)
  DATA_GO_KR_KEY                          공공데이터포털 '공공기관 채용정보' API (잡알리오: 기업·산업·수출입은행)

결과물: docs/data/news-recent.json(최근 8일), news-archive.json(그 이전), jobs.json, econ.json, meta.json
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
    # 금리 (817Y002 = 시장금리 일별)
    ("base",   "기준금리",          "%",  "722Y001", "D", "0101000"),
    ("ktb3",   "국고채 3년",        "%",  "817Y002", "D", "010200000"),
    ("ktb10",  "국고채 10년",       "%",  "817Y002", "D", "010210000"),
    ("bankb",  "은행채(산금채 1년)", "%", "817Y002", "D", "010260000"),
    ("corp3",  "회사채 3년 AA-",    "%",  "817Y002", "D", "010300000"),
    ("cd91",   "CD 91일",           "%",  "817Y002", "D", "010502000"),
    # 환율·주식
    ("usdkrw", "원/달러 환율",      "원", "731Y001", "D", "0000001"),
    ("jpykrw", "원/100엔 환율",     "원", "731Y001", "D", "0000002"),
    ("kospi",  "코스피",            "pt", "802Y001", "D", "0001000"),
    ("kosdaq", "코스닥",            "pt", "802Y001", "D", "0089000"),
    # 원자재·물가 (902Y003 = 국제상품가격, 월평균)
    ("gold",   "금 (국제)",         "$/oz",   "902Y003", "M", "040101"),
    ("wti",    "국제유가 WTI",      "$/배럴", "902Y003", "M", "010101"),
    ("cpi",    "소비자물가 상승률", "%",  "901Y009", "M", "0"),   # 지수를 받아 전년동월비로 변환
]

session = requests.Session()
session.headers.update(UA)


SECRET_ENVS = ["NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET", "SARAMIN_ACCESS_KEY", "ECOS_API_KEY", "DATA_GO_KR_KEY"]


def _secrets() -> list[str]:
    """로그에서 가릴 값: 키 원문과 URL 인코딩된 형태 (오류 메시지에 요청 주소가 찍힐 수 있다)"""
    vals = set()
    for name in SECRET_ENVS:
        v = os.getenv(name)
        if v:
            vals |= {v, urllib.parse.quote(v, safe=""), urllib.parse.quote_plus(v)}
    return sorted(vals, key=len, reverse=True)


def log(*a):
    msg = " ".join(str(x) for x in a)
    for s in _secrets():
        msg = msg.replace(s, "***")
    print(msg, file=sys.stderr, flush=True)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def save_rows(path: Path, rows: list[dict]):
    """뉴스처럼 큰 목록: 한 줄에 기사 하나씩, 공백 없이 저장 (파일 크기↓, git 변경 내역은 기사 단위로 보임)"""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = ",\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in rows)
    path.write_text("[\n" + body + "\n]\n", encoding="utf-8")


# 사이트가 처음 열릴 때 받는 '최근' 뉴스 범위(일). 나머지는 news-archive.json으로 나눠서 나중에 받는다.
RECENT_DAYS = 8


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
def google_news(bank: dict, extra: str, query: str | None = None, topic: str = "") -> list[dict]:
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
        out.append({"bank": bank["id"], "title": title, "source": source, "topic": topic,
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
            "via": "사람인",
        })
    return out


# ---------------------------------------------------------------- 채용공고: 은행 공식 채용사이트 (키 불필요)
DT_RE = re.compile(r"(\d{4})[.\-/]?(\d{2})[.\-/]?(\d{2})(?:[ T]+(\d{1,2}):(\d{2}))?")


def parse_dt(s: str | None) -> str | None:
    """'2026-10-14T14:00:59' / '2026.10.14 14:00 (접수마감)' / '20261014' → ISO(KST). 시각이 없으면 23:59"""
    m = DT_RE.search(s or "")
    if not m:
        return None
    y, mo, d, hh, mm = m.groups()
    try:
        return datetime(int(y), int(mo), int(d), int(hh or 23), int(mm or 59), tzinfo=KST).isoformat()
    except ValueError:
        return None


def job(bank, title, link, start=None, deadline=None, via="", **extra):
    return {"bank": bank["id"], "title": clean(title), "link": link,
            "posted": start, "deadline": deadline, "via": via, **extra}


def jobs_jobflex(bank: dict, host: str) -> list[dict]:
    """recruiter.co.kr(잡플렉스) 기반 채용사이트: 신한·하나·부산·경남·케이뱅크 등"""
    out, page = [], 1
    while page <= 5:
        r = session.post("https://api-recruiter.recruiter.co.kr/position/v1/jobflex",
                         headers={"Content-Type": "application/json", "prefix": host,
                                  "Origin": f"https://{host}", "Referer": f"https://{host}/career/jobs"},
                         json={"pageableRq": {"page": page, "size": 50, "sort": ["JOBFLEX_SORT"]},
                               "filter": {"keyword": "", "tagSnList": [], "jobGroupSnList": [], "careerTypeList": [],
                                          "regionSnList": [], "submissionStatusList": [], "openStatusList": [],
                                          "resumeLanguageTypeList": []}},
                         timeout=20)
        r.raise_for_status()
        js = r.json()
        for p in js.get("list", []):
            tags = [t.get("tagName", "") for t in p.get("tagList") or []]
            out.append(job(bank, p["title"], f"https://{host}/career/jobs/{p['positionSn']}",
                           parse_dt(p.get("startDateTime")), parse_dt(p.get("endDateTime")), "공식 채용사이트",
                           career={"NEW": "신입", "CAREER": "경력", "NONE": "경력무관"}.get(p.get("careerType"), ""),
                           job_type=" · ".join(tags)))
        if page >= (js.get("pagination") or {}).get("totalPages", 1):
            break
        page += 1
    return out


INCRUIT_RE = re.compile(r'href="(?:https?:)?//recruit\.incruit\.com/([\w-]+)/job/(\d+)"[^>]*>\s*'
                        r'<strong class="title">(.*?)</strong>.*?<em>(.*?)</em>', re.S)


def jobs_incruit(bank: dict, slug: str) -> list[dict]:
    """인크루트 채용사이트: 국민·우리·농협·산업은행"""
    r = session.get(f"https://recruit.incruit.com/{slug}/job/", timeout=20)
    r.raise_for_status()
    html_ = r.content.decode("euc-kr", errors="replace")
    out = []
    for s, jid, title, period in INCRUIT_RE.findall(html_):
        a, _, b = clean(period).partition("~")
        out.append(job(bank, title, f"https://recruit.incruit.com/{s}/job/{jid}",
                       parse_dt(a.strip()), parse_dt(b.strip()), "공식 채용사이트"))
    return out


NH_CTA_RE = re.compile(r'<a href="((?:https?:)?//[^"]*viewhire\.asp\?projectid=\d+)"[^>]*>(.*?)</a>', re.S)
NH_START_RE = re.compile(r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})")
NH_END_RE = re.compile(r"~\s*(?:(\d{4})\.\s*)?(\d{1,2})\.\s*(\d{1,2})\.?(?:\s*(\d{1,2}):(\d{2}))?")


def jobs_nhbank(bank: dict, url: str) -> list[dict]:
    """NH농협은행 채용 홈페이지(nhbank.incruit.com): 5급·6급 신규직원 공채가 여기에만 올라온다.
    첫 화면의 '공고 버튼'(nh-visual__cta)을 읽는다. 마감된 공고는 링크 대신 alert라서 자동으로 빠진다."""
    r = session.get(url, timeout=20)
    r.raise_for_status()
    html_ = r.content.decode("euc-kr", errors="replace")
    out = []
    for link, inner in NH_CTA_RE.findall(html_):
        tag_ = re.search(r'cta-tag">(.*?)<', inner, re.S)
        tit = re.search(r'cta-tit">(.*?)<', inner, re.S)
        date = re.search(r'cta-date">(.*?)<', inner, re.S)
        year = re.search(r"(\d{4})년", inner)
        if not tit:
            continue
        title = " ".join(x for x in [f"{year.group(1)}년" if year else "", clean(tag_.group(1)) if tag_ else "",
                                     clean(tit.group(1))] if x)
        period = clean(date.group(1)) if date else ""
        start = deadline = None
        if (s := NH_START_RE.search(period)):
            start = datetime(int(s[1]), int(s[2]), int(s[3]), 0, 0, tzinfo=KST).isoformat()
        if (e := NH_END_RE.search(period)):
            y = int(e[1] or (s[1] if s else NOW.year))
            deadline = datetime(y, int(e[2]), int(e[3]), int(e[4] or 23), int(e[5] or 59), tzinfo=KST).isoformat()
        out.append(job(bank, title, link if link.startswith("http") else "https:" + link, start, deadline,
                       "공식 채용사이트", career="신입", job_type="정규직"))
    return out


def jobs_kakaobank(bank: dict) -> list[dict]:
    out, page = [], 1
    while page <= 10:
        r = session.post("https://recruit.kakaobank.com/api/recruits",
                         headers={"Content-Type": "application/json", "Referer": "https://recruit.kakaobank.com/jobs"},
                         json={"pageNumber": page, "pageSize": 50}, timeout=20)
        r.raise_for_status()
        js = r.json()
        for p in js.get("list", []):
            url = p.get("recruitNoticeUrl") or ""
            out.append(job(bank, p["recruitNoticeName"], url if url.startswith("http") else "https://" + url,
                           parse_dt(p.get("receiveStartDatetime")), parse_dt(p.get("receiveEndDatetime")),
                           "공식 채용사이트", job_type=p.get("recruitClassName", "")))
        if page >= (js.get("paging") or {}).get("totalPages", 1):
            break
        page += 1
    return out


def jobs_toss(bank: dict, company: str) -> list[dict]:
    r = session.get("https://api-public.toss.im/api/v3/ipd-eggnog/career/job-groups", timeout=40)
    r.raise_for_status()
    out = []
    for g in r.json().get("success", []):
        pj = g.get("primary_job") or {}
        meta = {m.get("name", ""): m.get("value") for m in pj.get("metadata") or []}
        if not any(v == company for k, v in meta.items() if "자회사" in k):
            continue
        if any(v is True for k, v in meta.items() if "미노출" in k):  # 커리어 페이지에 숨긴 공고
            continue
        # 대부분 상시 채용이라 마감일이 없고, 일부만 '클로징 일자'(+ 시각)가 있다
        close_d = next((v for k, v in meta.items() if "클로징 일자" in k and v), None)
        close_t = next((v for k, v in meta.items() if "클로징 시각" in k and v), None)
        deadline = parse_dt(f"{close_d} {close_t}" if close_d and close_t else close_d) if close_d else None
        out.append(job(bank, g.get("title", ""), pj.get("absolute_url", "https://toss.im/career/jobs"),
                       None, deadline, "공식 채용사이트", job_type=meta.get("Employment_Type") or ""))
    return out


_ALIO_CACHE: list | None = None


def jobs_alio(bank: dict, inst: str) -> list[dict]:
    """잡알리오(공공기관 채용정보) - 공공데이터포털 API. 기업·산업·수출입은행"""
    global _ALIO_CACHE
    key = os.getenv("DATA_GO_KR_KEY")
    if not key:
        return []
    if _ALIO_CACHE is None:
        # 중간에 실패하면 캐시를 채우지 않는다 (다음 은행에서 일부만 받은 목록을 쓰지 않도록)
        rows_all, page = [], 1
        while page <= 20:
            r = session.get("https://apis.data.go.kr/1051000/recruitment/list",
                            params={"serviceKey": key, "resultType": "json", "ongoingYn": "Y",
                                    "numOfRows": 100, "pageNo": page}, timeout=30)
            r.raise_for_status()
            try:
                js = r.json()
            except ValueError:  # 키 오류 등은 JSON 대신 XML/문자열로 온다
                raise RuntimeError(f"JSON이 아닌 응답: {r.text[:200]}") from None
            if page == 1:
                sample = (js.get("result") or [{}])[0]
                sample = sample.get("item", sample) if isinstance(sample, dict) else {}
                log(f"  [alio] resultCode={js.get('resultCode')} totalCount={js.get('totalCount')} "
                    f"날짜 예시: {sample.get('pbancBgngYmd')} ~ {sample.get('pbancEndYmd')}")
            if str(js.get("resultCode")) not in ("0", "200", "00"):
                raise RuntimeError(js.get("resultMsg") or js)
            rows = [x.get("item", x) for x in js.get("result") or []]
            rows_all += rows
            if len(rows) < 100 or len(rows_all) >= int(js.get("totalCount") or 0):
                break
            page += 1
        _ALIO_CACHE = rows_all
        log(f"  [alio] 진행 중 공공기관 공고 {len(_ALIO_CACHE)}건")
    out = []
    for p in _ALIO_CACHE:
        if inst not in (p.get("instNm") or ""):
            continue
        src = p.get("srcUrl") or ""
        link = src if src.startswith("http") else f"https://job.alio.go.kr/recruitview.do?idx={p.get('recrutPblntSn')}"
        out.append(job(bank, p.get("recrutPbancTtl", ""), link, parse_dt(p.get("pbancBgngYmd")),
                       parse_dt(p.get("pbancEndYmd")), "잡알리오",
                       career=p.get("recrutSeNm") or "", job_type=p.get("hireTypeNmLst") or "",
                       location=p.get("workRgnNmLst") or ""))
    return out


JOB_SOURCES = {"jobflex": lambda b, s: jobs_jobflex(b, s["host"]),
               "incruit": lambda b, s: jobs_incruit(b, s["slug"]),
               "nhbank": lambda b, s: jobs_nhbank(b, s["url"]),
               "kakaobank": lambda b, s: jobs_kakaobank(b),
               "toss": lambda b, s: jobs_toss(b, s["company"]),
               "alio": lambda b, s: jobs_alio(b, s["inst"])}


REPORT: list[tuple[str, str, str]] = []  # (은행, 수집처, 결과) → Actions 실행 요약 표


def bank_jobs(bank: dict) -> tuple[list[dict], bool]:
    """(공고 목록, 성공 여부)"""
    out, ok = [], True
    for src in bank.get("jobs", []):
        if src["type"] == "alio" and not os.getenv("DATA_GO_KR_KEY"):
            REPORT.append((bank["name"], "alio", "키 없음 (건너뜀)"))
            continue
        try:
            got = JOB_SOURCES[src["type"]](bank, src)
            log(f"  [{src['type']}] {bank['name']}: {len(got)}건")
            REPORT.append((bank["name"], src["type"], f"{len(got)}건"))
            out += got
        except Exception as e:  # noqa: BLE001
            ok = False
            log(f"  [{src['type']}] {bank['name']} 실패: {e}")
            REPORT.append((bank["name"], src["type"], "실패 (이전 공고 유지)"))
    if not bank.get("jobs"):
        REPORT.append((bank["name"], "-", "자동 수집처 없음"))
    out += saramin_jobs(bank)
    return out, ok


def write_summary(lines: list[str]):
    """GitHub Actions 실행 화면의 Summary에 표로 남긴다 (로컬 실행에서는 아무것도 안 함)"""
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    text = "\n".join(lines)
    for s in _secrets():
        text = text.replace(s, "***")
    with open(path, "a", encoding="utf-8") as f:
        f.write(text + "\n")


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
                REPORT.append(("경제지표", f"ecos {name}", f"{len(pts)}개, 최근 {pts[-1][0]} = {pts[-1][1]}"))
            else:
                log(f"  [ecos] {name}: 값 없음 (항목 코드 {item} 확인 필요, 기존 값 유지)")
                REPORT.append(("경제지표", f"ecos {name}", "값 없음 (기존 값 유지)"))
        except Exception as e:  # noqa: BLE001
            log(f"  [ecos] {name} 실패 (기존 값 유지): {e}")
            REPORT.append(("경제지표", f"ecos {name}", "실패 (기존 값 유지)"))
        time.sleep(0.2)
    return {"updated": NOW.isoformat(), "series": series,
            "order": [i[0] for i in INDICATORS]}


# ---------------------------------------------------------------- 병합·저장
# 은행 뉴스 내용 분류: topics.json 위에서부터 처음 맞는 카테고리 (없으면 etc)
TOPICS = json.loads((ROOT / "topics.json").read_text(encoding="utf-8"))
SPAM_RE = re.compile(r"토토|카지노|슬롯|바카라|먹튀|사설\s?사이트")


def news_topic(title: str) -> str:
    for t in TOPICS:
        if any(w in title for w in t["words"]) and not any(w in title for w in t.get("except", [])):
            return t["id"]
    return "etc"


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
        if SPAM_RE.search(n["title"]):  # 은행 이름을 끼워 넣은 도박 광고 글
            continue
        k = (n["bank"], norm_key(n["title"]))
        if k in seen:
            continue
        seen.add(k)
        n["cat"] = news_topic(n["title"])
        n["job"] = n["cat"] == "job"
        n["econ"] = n["bank"] == "macro" or is_econ_news(n["title"])
        merged.append(n)
    merged.sort(key=lambda x: x["date"], reverse=True)
    return merged


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true", help="최근 6개월 뉴스를 주 단위로 채운다 (최초 1회)")
    args = ap.parse_args()

    banks = json.loads((ROOT / "banks.json").read_text(encoding="utf-8"))
    jobs_path = DATA / "jobs.json"
    recent_path, archive_path, legacy_path = DATA / "news-recent.json", DATA / "news-archive.json", DATA / "news.json"
    # 예전 한 파일(news.json)도 읽어서 옮겨 담는다 (처음 한 번)
    old_news = load_json(recent_path, []) + load_json(archive_path, []) + load_json(legacy_path, [])
    old_jobs = load_json(jobs_path, [])

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
        got, ok = bank_jobs(b)
        if not ok:  # 수집 실패한 은행은 이전 공고를 유지
            got += [j for j in old_jobs if j["bank"] == b["id"]]
        jobs += got
        time.sleep(0.3)

    # 거시경제 뉴스
    log("· 경제(거시)")
    for q in MACRO["queries"]:
        fresh += google_news(MACRO, "when:30d" if args.backfill else "when:7d", query=f'"{q}"', topic=q)
        time.sleep(0.3)
    fresh += naver_news(MACRO, pages=1)

    news = merge_news(old_news, fresh)
    split = (NOW - timedelta(days=RECENT_DAYS)).isoformat()
    recent = [n for n in news if n["date"] >= split]
    archive = [n for n in news if n["date"] < split]
    for n in news:
        n.pop("econ", None)  # 사이트에서 쓰지 않는 값이라 저장하지 않음
    save_rows(recent_path, recent)
    save_rows(archive_path, archive)
    if legacy_path.exists():
        legacy_path.unlink()
    log(f"  뉴스 파일: 최근 {RECENT_DAYS}일 {len(recent)}건 / 나머지 {len(archive)}건")

    # 채용공고: 마감되지 않은 것만. 사람인 키가 없으면 기존 파일 유지
    # 채용공고: 마감 전(또는 상시) 공고만, 링크 기준 중복 제거, 마감 임박 순
    today = NOW.isoformat()
    jobs = [j for j in jobs if j.get("title") and (not j.get("deadline") or j["deadline"] >= today)]
    uniq = {j["link"]: j for j in jobs}
    jobs = sorted(uniq.values(), key=lambda j: (j.get("deadline") or "9999", j["bank"]))
    save_json(jobs_path, jobs)

    econ = economy(load_json(DATA / "econ.json", {}))
    save_json(DATA / "econ.json", econ)

    meta = {
        "updated": NOW.isoformat(),
        "sources": {
            "google_news": True,
            "naver": bool(os.getenv("NAVER_CLIENT_ID")),
            "saramin": bool(os.getenv("SARAMIN_ACCESS_KEY")),
            "ecos": bool(os.getenv("ECOS_API_KEY")),
            "alio": bool(os.getenv("DATA_GO_KR_KEY")),
        },
        "banks": banks,
        "topics": [{"id": t["id"], "name": t["name"]} for t in TOPICS] + [{"id": "etc", "name": "기타"}],
    }
    save_json(DATA / "meta.json", meta)
    log(f"완료: 뉴스 {len(news)}건 (신규 수집 {len(fresh)}건), 채용공고 {len(jobs)}건")

    per_bank = {}
    for j in jobs:
        per_bank[j["bank"]] = per_bank.get(j["bank"], 0) + 1
    write_summary([
        f"## 수집 결과 ({NOW:%Y-%m-%d %H:%M} KST)",
        f"- 뉴스 {len(news)}건 누적 (이번 신규 {len(fresh)}건)",
        f"- 진행 중 채용공고 {len(jobs)}건 · 사용한 키: "
        + (", ".join(k for k, v in meta["sources"].items() if v) or "없음"),
        "",
        "| 은행 | 수집처 | 결과 | 저장된 공고 |",
        "| --- | --- | --- | --- |",
        *[f"| {b} | {s} | {r} | "
          + (str(per_bank.get(next((x['id'] for x in banks if x['name'] == b), ''), 0)) if b != "경제지표" else "")
          + " |" for b, s, r in REPORT],
    ])


if __name__ == "__main__":
    main()
