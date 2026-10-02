# -*- coding: utf-8 -*-
"""
3분봉 차트 누적 저장소 (SQLite).

KIS 3분봉 API는 1회 30개만 내려주므로, 3-State HMM 학습에 필요한
역사 분봉을 로컬 DB에 누적한다.

- 저장 형식: SQLite (bars 테이블). 종목별 JSON 파일보다 작고,
  add() 할 때 전체를 다시 쓰지 않으므로 누적 속도가 빠르다.
- 봉 식별자: (ticker, date, time). time 은 "HHMMSS" 로 거래일이 포함되지 않으므로
  날짜가 없으면 다른 거래일의 같은 시각이 중복으로 제거되어 누적률이 멈춘다.
- 정방향(과거→최신) 조회, 종목별 최근 MAX_BARS개만 보관

ChartStore(JSON) 는 마이그레이션 전용으로 남겨 두었다. 신규 저장은 ChartDB 를 쓴다.
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta

import pandas as pd

DEFAULT_STORE_FILE = "chart_store.db"
LEGACY_STORE_FILE = "chart_store.json"
MAX_BARS = 500          # 종목별 보관 봉 수
REQUIRED_COLUMNS = ["date", "time", "open", "high", "low", "close", "volume"]

MARKET_OPEN_MIN = 9 * 60
MARKET_CLOSE_MIN = 20 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    ticker     TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    bar_time   TEXT NOT NULL,
    open       REAL,
    high       REAL,
    low        REAL,
    close      REAL,
    volume     REAL,
    PRIMARY KEY (ticker, trade_date, bar_time)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS tickers (
    ticker     TEXT PRIMARY KEY,
    updated_at TEXT
);
"""


def current_trade_date(now=None):
    """3분봉이 속한 거래일 추정.

    KIS 분봉 API는 당일 구간(09:00~20:00)만 내려주고 날짜를 주지 않는다.
    장 시작 전(09:00 이전)에 조회하면 아직 봉이 생성되지 않았으므로 직전
    거래일을 사용한다. 주말은 금요일로 보정한다.
    """
    now = now or datetime.now()

    if now.hour * 60 + now.minute < MARKET_OPEN_MIN:
        day = now.date() - timedelta(days=1)
    else:
        day = now.date()

    while day.weekday() >= 5:      # 토(5), 일(6)
        day -= timedelta(days=1)

    return day.strftime("%Y-%m-%d")


class ChartStore:
    """구 JSON 저장소 — 마이그레이션 읽기 전용으로만 사용

    신규 저장은 ChartDB 를 사용한다. (JSON 은 add() 마다 전체를 다시
    직렬화하므로 종목 수가 늘면 저장과 메모리가 함께 부담이 된다)
    """

    def __init__(self, store_file=LEGACY_STORE_FILE):
        self.store_file = store_file
        self.data = {}
        self.load()

    # ==============================
    # 💾 입출력
    # ==============================
    def load(self):
        if not os.path.exists(self.store_file):
            self.data = {}
            return

        try:
            with open(self.store_file, "r", encoding="utf-8-sig") as f:
                self.data = json.load(f)
        except Exception as e:
            print(f"⚠️ 차트 저장소 로드 실패 ({self.store_file}): {e}. 새로 시작합니다.")
            self.data = {}

        self._migrate()

    def _migrate(self):
        """date 컬럼이 없는 기존 저장소에 날짜를 채워 넣는다."""
        changed = False

        for ticker, payload in self.data.items():
            bars = payload.get("bars", [])
            updated = payload.get("updated_at", "")
            day = updated[:10] if updated else current_trade_date()

            for bar in bars:
                if not bar.get("date"):
                    bar["date"] = day
                    changed = True

        if changed:
            self.save()

    def save(self):
        with open(self.store_file, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    # ==============================
    # 📦 조회 / 갱신
    # ==============================
    def get_df(self, ticker):
        """누적된 3분봉을 정방향(과거→최신) DataFrame으로 반환"""
        bars = self.data.get(ticker, {}).get("bars", [])
        if not bars:
            return pd.DataFrame(columns=REQUIRED_COLUMNS)

        df = pd.DataFrame(bars)
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors='coerce')

        df = df.dropna(subset=['close', 'volume'])
        df = df.drop_duplicates(subset=['date', 'time'], keep='last')
        return df.sort_values(['date', 'time']).reset_index(drop=True)

    def count(self, ticker):
        return len(self.get_df(ticker))

    def add(self, ticker, rows):
        """새 분봉을 누적하고, 새로 추가된 봉 수를 반환

        봉 식별은 (date, time) 기준이다. date 가 없는 row 는 현재 거래일로 채운다.
        """
        if not rows:
            return 0

        existing = {(str(bar.get('date')), str(bar.get('time')))
                    for bar in self.data.get(ticker, {}).get("bars", [])}

        new_bars = []
        for row in rows:
            date = str(row.get('date') or current_trade_date())
            key = (date, str(row.get('time')))

            if key in existing:
                continue

            if row.get('close') is None:
                continue

            bar = {col: row.get(col) for col in REQUIRED_COLUMNS}
            bar['date'] = date
            new_bars.append(bar)
            existing.add(key)

        if not new_bars:
            return 0

        bars = self.data.setdefault(ticker, {"bars": []})["bars"]
        bars.extend(new_bars)
        bars.sort(key=lambda b: (str(b.get('date')), int(b['time'])))

        if len(bars) > MAX_BARS:
            del bars[:-MAX_BARS]

        self.data[ticker]["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.save()
        return len(new_bars)

    def summary(self):
        """보유 종목별 누적 봉 현황"""
        rows = [(ticker, len(payload.get("bars", [])), payload.get("updated_at", ""))
                for ticker, payload in self.data.items()]

        if not rows:
            return rows

        return sorted(rows, key=lambda r: r[1], reverse=True)


# ==============================
# 💾 SQLite 저장소
# ==============================
def _to_float(value):
    """숫자로 변환하고 실패하면 None (SQL NULL 저장)"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_time(value):
    """'93000' → '093000' 형태의 6자리 문자열로 맞춘다

    문자열 정렬이 시간순과 같아지도록 0 으로 채운다.
    """
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return digits.zfill(6) if digits else ""


class ChartDB:
    """3분봉 누적 SQLite 저장소

    ChartStore 와 같은 API(get_df / add / count / summary)를 제공하므로
    호출부가 바뀌지 않는다. 차이는 add() 가 전체 파일을 다시 쓰지 않고
    해당 종목 행만 갱신한다는 점이다.
    """

    def __init__(self, db_file=DEFAULT_STORE_FILE, max_bars=MAX_BARS):
        self.db_file = db_file
        self.store_file = db_file      # 로그 출력 호환용 속성명
        self.max_bars = max_bars

        parent = os.path.dirname(os.path.abspath(db_file))
        if parent:
            os.makedirs(parent, exist_ok=True)

        self.conn = sqlite3.connect(db_file)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ==============================
    # 📦 조회
    # ==============================
    def get_df(self, ticker):
        """누적된 3분봉을 정방향(과거→최신) DataFrame으로 반환"""
        rows = self.conn.execute(
            "SELECT trade_date, bar_time, open, high, low, close, volume "
            "FROM bars WHERE ticker = ? ORDER BY trade_date, bar_time",
            (ticker,),
        ).fetchall()

        if not rows:
            return pd.DataFrame(columns=REQUIRED_COLUMNS)

        df = pd.DataFrame([dict(r) for r in rows]).rename(
            columns={"trade_date": "date", "bar_time": "time"}
        )
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df.dropna(subset=["close", "volume"])
        df = df.drop_duplicates(subset=["date", "time"], keep="last")
        return df.sort_values(["date", "time"]).reset_index(drop=True)

    def count(self, ticker):
        """보유 봉 수 (close/volume 이 없는 봉 제외)"""
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM bars "
            "WHERE ticker = ? AND close IS NOT NULL AND volume IS NOT NULL",
            (ticker,),
        ).fetchone()[0])

    def tickers(self):
        return [r[0] for r in self.conn.execute(
            "SELECT ticker FROM tickers ORDER BY ticker").fetchall()]

    def summary(self, limit=None):
        """보유 종목별 누적 봉 현황 (봉 수 내림차순)"""
        sql = ("SELECT t.ticker, "
               "       SUM(CASE WHEN b.close IS NOT NULL AND b.volume IS NOT NULL"
               "                THEN 1 ELSE 0 END) AS bars, "
               "       t.updated_at "
               "FROM tickers t LEFT JOIN bars b ON b.ticker = t.ticker "
               "GROUP BY t.ticker ORDER BY bars DESC, t.ticker")
        if limit:
            sql += f" LIMIT {int(limit)}"

        return [(r["ticker"], int(r["bars"] or 0), r["updated_at"] or "")
                for r in self.conn.execute(sql).fetchall()]

    # ==============================
    # ✍️ 갱신
    # ==============================
    def add(self, ticker, rows):
        """새 분봉을 누적하고, 새로 추가된 봉 수를 반환

        봉 식별은 (date, time) 기준이며 같은 키가 이미 있으면 값만 갱신한다.
        date 가 없는 row 는 현재 거래일로 채운다.
        """
        if not rows:
            return 0

        trade_date = current_trade_date()
        payload = []

        for row in rows:
            if row.get("close") is None:
                continue

            bar_time = _normalize_time(row.get("time"))
            if not bar_time:
                continue

            payload.append((
                ticker,
                str(row.get("date") or trade_date),
                bar_time,
                _to_float(row.get("open")),
                _to_float(row.get("high")),
                _to_float(row.get("low")),
                _to_float(row.get("close")),
                _to_float(row.get("volume")),
            ))

        if not payload:
            return 0

        before = self.count(ticker)

        with self.conn:
            self.conn.executemany(
                "INSERT INTO bars (ticker, trade_date, bar_time, "
                "                   open, high, low, close, volume) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(ticker, trade_date, bar_time) DO UPDATE SET "
                "    open = excluded.open, high = excluded.high, "
                "    low = excluded.low, close = excluded.close, "
                "    volume = excluded.volume",
                payload,
            )
            self.conn.execute(
                "INSERT INTO tickers (ticker, updated_at) VALUES (?, ?) "
                "ON CONFLICT(ticker) DO UPDATE SET updated_at = excluded.updated_at",
                (ticker, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            self._trim(ticker)

        return self.count(ticker) - before

    def _trim(self, ticker):
        """종목별 최근 max_bars 개만 남긴다"""
        self.conn.execute(
            "DELETE FROM bars WHERE ticker = :t "
            "AND (trade_date || bar_time) NOT IN ("
            "    SELECT trade_date || bar_time FROM bars WHERE ticker = :t "
            "    ORDER BY trade_date DESC, bar_time DESC LIMIT :n)",
            {"t": ticker, "n": self.max_bars},
        )

    # ==============================
    # 🔧 관리
    # ==============================
    def load(self):
        """JSON 저장소와의 API 호환용 no-op"""
        return self

    def save(self):
        """SQLite 는 즉시 반영되므로 no-op"""
        return None

    def file_size(self):
        try:
            return os.path.getsize(self.db_file)
        except OSError:
            return 0

    def vacuum(self):
        """삭제된 봉이 남긴 여분을 되돌려 파일 크기를 줄인다"""
        self.conn.execute("VACUUM")

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ==============================
# 🔀 마이그레이션 / 팩토리
# ==============================
def migrate_json_to_db(json_file, db_file, max_bars=MAX_BARS, quiet=False):
    """구 JSON 저장소를 SQLite 로 옮기고 옮긴 종목 수를 반환

    이미 DB 에 같은 키의 봉이 있으면 값만 갱신되므로 재실행해도 안전하다.
    """
    if not os.path.exists(json_file):
        return 0

    legacy = ChartStore(json_file)

    with ChartDB(db_file, max_bars=max_bars) as db:
        moved = 0
        for ticker, payload in legacy.data.items():
            bars = payload.get("bars", [])
            if not bars:
                continue

            rows = [{col: bar.get(col) for col in REQUIRED_COLUMNS}
                    for bar in bars]
            moved += db.add(ticker, rows)

    if not quiet:
        size_note = ""
        if os.path.exists(json_file):
            json_mb = os.path.getsize(json_file) / 1024 / 1024
            db_mb = os.path.getsize(db_file) / 1024 / 1024
            size_note = f"  ({json_mb:.2f}MB → {db_mb:.2f}MB)"
        print(f"🗄️ [{json_file}] → [{db_file}] {moved}봉 이관{size_note}")

    return moved


def open_store(path):
    """저장 경로에 맞춰 적절한 저장소를 연다

    .json 이면 같은 이름의 .db 로 마이그레이션 후 SQLite 로 연다.
    기존 JSON 경로를 그대로 넘겨도 데이터가 유실되지 않는다.
    """
    if path.endswith(".json"):
        db_file = path[:-len(".json")] + ".db"
        migrate_json_to_db(path, db_file)
        return ChartDB(db_file)

    return ChartDB(path)
