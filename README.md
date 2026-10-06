# H bank (은행 취업 허브)

**사이트: https://uouclaudecode-cpu.github.io/hbank/**

은행별 **채용 정보(공고+채용 소식)**, **최근 1주·1개월·6개월 뉴스(전체/채용/경제)**, **국내 경제지표(기준금리·국고채·CD·환율·코스피·물가)**를 한 화면에서 볼 수 있는 웹사이트입니다.
GitHub Actions가 6시간마다 데이터를 수집하고, GitHub Pages가 사이트를 호스팅합니다. **서버와 비용은 필요 없습니다.**

## 구성

| 경로 | 역할 |
|---|---|
| `banks.json` | 대상 은행 목록 (이름, 검색어, 공식 채용 홈페이지, 키컬러 `color`). 은행 추가·수정은 여기서만 하면 됩니다 |
| `collector/collect.py` | 뉴스·채용공고·경제지표 수집 → `docs/data/*.json` 저장 (뉴스는 누적, 183일 지나면 삭제) |
| `docs/index.html` | 웹사이트 (경제지표·주요 경제 뉴스, 은행 탭, 채용공고 D-day, 은행 뉴스 전체/채용) |
| `.github/workflows/collect.yml` | 6시간마다 자동 수집 |

## 데이터 출처

| 데이터 | 출처 | 키 필요 여부 |
|---|---|---|
| 뉴스 | 구글 뉴스 RSS | 필요 없음 (기본으로 동작) |
| 뉴스 (보강) | 네이버 검색 API | 선택. 넣으면 기사가 훨씬 많아짐 |
| 채용공고 (국민·우리·농협·산업) | 은행 공식 채용사이트(인크루트) | 필요 없음 |
| 채용공고 (신한·하나·부산·경남·케이뱅크) | 은행 공식 채용사이트(recruiter.co.kr) | 필요 없음 |
| 채용공고 (카카오뱅크·토스뱅크) | 각 사 채용사이트 | 필요 없음 |
| 채용공고 (기업·산업·수출입은행) | 잡알리오 (공공데이터포털 '공공기관 채용정보 조회서비스') | `DATA_GO_KR_KEY` |
| 채용공고 (그 외 보강) | 사람인 오픈API | 선택 |
| 경제지표 | 한국은행 ECOS 오픈API | **경제지표 영역에 필요** (무료, 즉시 발급) |
| 거시경제 뉴스 | 구글 뉴스 RSS (+네이버) | 필요 없음. 뉴스란 '경제' 카테고리에 표시 |

## 처음 설정하기 (약 15분)

1. **GitHub 저장소 만들기**: github.com에서 New repository를 만듭니다 (예: `hbank`, Public). 이 폴더의 파일을 전부 업로드합니다 (`.github` 폴더 포함).
2. **API 키 등록 (선택)**: 저장소 **Settings → Secrets and variables → Actions → New repository secret**
   - `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`: [네이버 개발자센터](https://developers.naver.com/apps/#/register)에서 애플리케이션을 등록할 때 사용 API로 '검색'을 선택하면 발급됩니다.
   - `SARAMIN_ACCESS_KEY`: [사람인 오픈API](https://oapi.saramin.co.kr)에서 신청합니다 (승인까지 며칠 걸릴 수 있음).
   - `DATA_GO_KR_KEY`: [공공데이터포털](https://www.data.go.kr/data/15125273/openapi.do)에서 '활용신청' → 자동 승인 → 마이페이지의 **일반 인증키(Decoding)**를 등록합니다.
   - `ECOS_API_KEY`: [한국은행 ECOS 오픈API](https://ecos.bok.or.kr/api/)에서 회원가입 후 '인증키 신청'을 하면 바로 발급됩니다.
3. **쓰기 권한 켜기**: **Settings → Actions → General → Workflow permissions**에서 *Read and write permissions*를 선택하고 저장합니다.
4. **6개월치 뉴스 채우기**: **Actions 탭 → 데이터 수집 → Run workflow**에서 *최근 6개월 뉴스 채우기*를 체크하고 실행합니다 (10~20분 소요).
5. **사이트 공개**: **Settings → Pages → Branch: `main` / 폴더: `/docs`** 로 저장합니다. 1~2분 뒤 `https://<아이디>.github.io/hbank/`에서 사이트가 열립니다.

이후에는 6시간마다 자동으로 갱신됩니다.

## 내 컴퓨터에서 실행해 보기

```bash
pip install -r requirements.txt
python collector/collect.py            # 수집 (키는 환경변수로 넣어도 되고, 없어도 됨)
python -m http.server -d docs 8000     # http://localhost:8000 접속
```

## 자주 바꾸는 것

- **채용공고 수집처**: `banks.json`의 `jobs` (`jobflex`=recruiter.co.kr 주소, `incruit`=인크루트 채용사이트 이름, `alio`=잡알리오 기관명)
- **은행 추가**: `banks.json`에 한 줄 추가 (`aliases`는 제목에 들어가야 하는 은행명입니다)
- **채용 홈페이지 주소 수정**: `banks.json`의 `recruit`. 일부 은행은 채용 전용 사이트를 찾지 못해 은행 대표 홈페이지로 넣어 두었습니다 (SC제일, 씨티, iM, IBK, KDB, 수출입, 부산, 경남, 케이뱅크). 실제 채용 페이지 주소로 바꿔 주세요.
- **'채용'·'경제' 분류 단어**: `collect.py`의 `JOB_WORDS`, `ECON_WORDS`
- **거시경제 뉴스 검색어**: `collect.py`의 `MACRO["queries"]`
- **경제지표 종류**: `collect.py`의 `INDICATORS` (ECOS 통계표·항목 코드). 지표가 비어 보이면 Actions 실행 로그의 `[ecos]` 줄에서 오류 메시지를 확인하고, ECOS 사이트의 '통계코드검색'으로 코드를 바꿔 주세요.
- **은행 색상**: `banks.json`의 `color`. 각 은행 대표색을 참고한 근사치이며, 글자색(흰색/검정)은 자동으로 맞춰집니다.
- **수집 주기**: `collect.yml`의 `cron`

## 참고

- 구글 뉴스 RSS는 검색 1회당 최대 100건, 네이버 API는 검색어당 최근 약 1,000건까지만 제공합니다. 그래서 매번 결과를 **누적 저장**해 6개월 범위를 유지합니다.
- 화면에는 기사 **제목·언론사·링크만** 표시합니다. 본문을 복제하지 마세요 (저작권).
- 채용공고 마감일은 바뀔 수 있으니 지원 전에 반드시 공식 홈페이지에서 확인하세요.
