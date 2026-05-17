# -*- coding: utf-8 -*-
"""
彩球每日/每週自動更新完整版
- 先讀取本機 CSV
- 抓台彩官方最新開獎頁 / 查詢頁 / 歷史下載頁（多來源容錯）
- 合併、去重複、排序
- 產生最新狀態 Dashboard

資料來源說明：
1) 官方年度下載檔通常只適合補歷史資料。
2) 每天/每週最新期別，優先抓官方最新開獎頁與查詢頁。
3) 若官方頁面改版或當下沒有更新，程式會留下 reason log，不會假裝成功。
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import time
import zipfile
import traceback
import webbrowser
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Iterable

import pandas as pd
import requests
import urllib3
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "lottery"
OUTPUT_DIR = ROOT / "output"
LOG_DIR = ROOT / "logs"
RAW_DIR = DATA_DIR / "raw"
for p in [DATA_DIR, OUTPUT_DIR, LOG_DIR, RAW_DIR]:
    p.mkdir(parents=True, exist_ok=True)

# 標準欄位：game, draw_no, draw_date, n1..n6, special, source, updated_at
GAME_FILES = {
    "539": DATA_DIR / "539.csv",
    "lotto": DATA_DIR / "lotto.csv",
    "power": DATA_DIR / "power.csv",
}
GAME_NAMES = {
    "539": ["今彩539", "今彩 539", "539"],
    "lotto": ["大樂透", "大樂透6/49", "大樂透 6/49"],
    "power": ["威力彩"],
}
OFFICIAL_URLS = {
    "today_last_number": "https://www.taiwanlottery.com/today_last_number/",
    "latest_result": "https://www.taiwanlottery.com/lotto/lotto_lastest_result/",
    "result_query": "https://www.taiwanlottery.com/lotto/result/4_d/",
    "history_download": "https://www.taiwanlottery.com/lotto/history/result_download/",
    # 中信舊頁，有時候比新版純文字更好解析，作為備援
    "ctbc_result_all": "https://lotto.ctbcbank.com/result_all.htm",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7",
}

# Windows / Python Store sometimes fails TaiwanLottery SSL verification
# with: Missing Subject Key Identifier. Use a safe fallback only when normal SSL fails.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
REQUEST_TIMEOUT = 25



def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(msg)
    with (LOG_DIR / "lottery_auto_update.log").open("a", encoding="utf-8") as f:
        f.write(f"[{now_str()}] {msg}\n")


def safe_get(url: str, timeout: int = REQUEST_TIMEOUT) -> Optional[str]:
    """GET with SSL fallback.

    Some Windows Python installations reject TaiwanLottery certificates with
    CERTIFICATE_VERIFY_FAILED / Missing Subject Key Identifier. We first try
    normal verification; if that specific SSL path fails, retry with
    verify=False so daily update can continue.
    """
    last_err = None
    for verify in (True, False):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout, verify=verify)
            r.raise_for_status()
            if not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = r.apparent_encoding or "utf-8"
            if not verify:
                log(f"[SSL fallback OK] {url}")
            return r.text
        except requests.exceptions.SSLError as e:
            last_err = e
            if verify:
                log(f"[SSL fallback] 一般驗證失敗，改用備援抓取: {url}")
                continue
            log(f"抓取失敗: {url} | {e}")
            return None
        except Exception as e:
            last_err = e
            log(f"抓取失敗: {url} | {e}")
            return None
    if last_err:
        log(f"抓取失敗: {url} | {last_err}")
    return None


def normalize_digits(s: str) -> str:
    trans = str.maketrans("０１２３４５６７８９", "0123456789")
    return str(s).translate(trans)


def clean_cell(value: object) -> str:
    """Normalize common empty values that may come from pandas / HTML tables."""
    if value is None:
        return ""
    s = normalize_digits(str(value)).strip()
    if s.lower() in {"", "nan", "none", "nat", "<na>", "null"}:
        return ""
    return s


def valid_int_text(value: object, min_v: int = 1, max_v: int = 49) -> str:
    s = clean_cell(value)
    if not re.fullmatch(r"\d{1,2}", s):
        return ""
    n = int(s)
    if min_v <= n <= max_v:
        return str(n)
    return ""


def roc_to_ad_date(s: str) -> Optional[str]:
    s = normalize_digits(s).strip()
    # 115/05/14, 115-05-14, 2026/05/14
    m = re.search(r"(?P<y>\d{2,4})[/-](?P<m>\d{1,2})[/-](?P<d>\d{1,2})", s)
    if not m:
        return None
    y, mo, d = int(m.group("y")), int(m.group("m")), int(m.group("d"))
    if y < 1911:
        y += 1911
    try:
        return date(y, mo, d).isoformat()
    except Exception:
        return None


def extract_numbers(text: str, max_n: int = 6) -> List[int]:
    text = normalize_digits(text)
    nums = []
    for n in re.findall(r"(?<!\d)(\d{1,2})(?!\d)", text):
        v = int(n)
        if 1 <= v <= 49 and v not in nums:
            nums.append(v)
        if len(nums) >= max_n:
            break
    return nums


def empty_df() -> pd.DataFrame:
    cols = ["game", "draw_no", "draw_date", "n1", "n2", "n3", "n4", "n5", "n6", "special", "source", "updated_at"]
    return pd.DataFrame(columns=cols)


def read_game_csv(game: str) -> pd.DataFrame:
    path = GAME_FILES[game]
    if not path.exists():
        return empty_df()
    try:
        df = pd.read_csv(path, dtype=str)
        # 補欄位
        for c in empty_df().columns:
            if c not in df.columns:
                df[c] = ""
        return df[list(empty_df().columns)]
    except Exception as e:
        log(f"讀取 CSV 失敗 {path}: {e}")
        return empty_df()


def clean_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return empty_df()
    for c in empty_df().columns:
        if c not in df.columns:
            df[c] = ""
    df = df[list(empty_df().columns)].copy()

    # 全欄位先清掉 pandas/HTML 常見空值，避免 Dashboard 出現 nan。
    for c in df.columns:
        df[c] = df[c].map(clean_cell)

    df["draw_no"] = df["draw_no"].map(clean_cell)
    df["draw_date"] = df["draw_date"].map(clean_cell)
    df["game"] = df["game"].map(clean_cell)

    for c in ["n1", "n2", "n3", "n4", "n5", "n6", "special"]:
        max_v = 39 if c != "special" and df["game"].eq("539").all() else 49
        df[c] = df[c].map(lambda x: valid_int_text(x, 1, 49))

    # 539 沒有第 6 顆與特別號，強制清空。
    is_539 = df["game"].eq("539")
    df.loc[is_539, "n6"] = ""
    df.loc[is_539, "special"] = ""

    # 沒期別時用日期+號碼做 key
    df = df[(df["game"] != "") & ((df["draw_no"] != "") | (df["draw_date"] != ""))]
    # 去重：優先用 game+draw_no；沒 draw_no 才用 date+numbers
    df["_key"] = df.apply(lambda r: f"{r.game}|NO|{r.draw_no}" if str(r.draw_no).strip() else f"{r.game}|DATE|{r.draw_date}|{r.n1}|{r.n2}|{r.n3}|{r.n4}|{r.n5}|{r.n6}|{r.special}", axis=1)
    df = df.drop_duplicates("_key", keep="last").drop(columns=["_key"])
    # 排序
    df["_date_sort"] = pd.to_datetime(df["draw_date"], errors="coerce")
    df["_no_sort"] = pd.to_numeric(df["draw_no"].str.extract(r"(\d+)")[0], errors="coerce")
    df = df.sort_values(["_date_sort", "_no_sort"], na_position="first").drop(columns=["_date_sort", "_no_sort"])
    return df.reset_index(drop=True)


def save_game_csv(game: str, df: pd.DataFrame) -> None:
    df = clean_df(df)
    df.to_csv(GAME_FILES[game], index=False, encoding="utf-8-sig")


def infer_game_from_text(text: str) -> Optional[str]:
    for game, names in GAME_NAMES.items():
        if any(name in text for name in names):
            return game
    return None


def row_from_values(game: str, draw_no: str, draw_date: str, nums: List[int], special: Optional[int], source: str) -> Dict[str, str]:
    nums = [int(n) for n in list(nums or []) if isinstance(n, int) or str(n).isdigit()]
    if game == "539":
        nums = [n for n in nums if 1 <= n <= 39][:5]
        special = None
    elif game == "lotto":
        nums = [n for n in nums if 1 <= n <= 49][:6]
        if special is not None and not (1 <= int(special) <= 49):
            special = None
    elif game == "power":
        # 威力彩：第一區 6 號，第二區 special；若只抓到7個，最後當 special
        clean_nums = [n for n in nums if 1 <= n <= 49]
        if special is None and len(clean_nums) >= 7:
            special = clean_nums[6]
            nums = clean_nums[:6]
        else:
            nums = clean_nums[:6]
        if special is not None and not (1 <= int(special) <= 8):
            # 第二區只允許 1~8，錯誤資料直接清掉。
            special = None
    row = {
        "game": clean_cell(game),
        "draw_no": clean_cell(draw_no),
        "draw_date": clean_cell(draw_date),
        "source": clean_cell(source),
        "updated_at": now_str(),
        "special": "" if special is None else str(int(special)),
    }
    for i in range(1, 7):
        row[f"n{i}"] = str(nums[i-1]) if i <= len(nums) else ""
    if game == "539":
        row["n6"] = ""
        row["special"] = ""
    return row


def parse_text_blocks_for_latest(html: str, source: str) -> List[Dict[str, str]]:
    """解析官方最新頁/一覽頁。這是容錯 regex，不綁死 HTML 結構。"""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    text = normalize_digits(text)
    rows: List[Dict[str, str]] = []

    # 先依遊戲名稱切片，每個 block 抓期別、日期、號碼
    game_markers = []
    for game, names in GAME_NAMES.items():
        for name in names:
            for m in re.finditer(re.escape(name), text):
                game_markers.append((m.start(), game, name))
    game_markers = sorted(game_markers, key=lambda x: x[0])
    for idx, (pos, game, name) in enumerate(game_markers):
        end = game_markers[idx + 1][0] if idx + 1 < len(game_markers) else min(len(text), pos + 1200)
        block = text[pos:end]
        # 日期
        d = roc_to_ad_date(block) or ""
        # 期別：常見 115000118 / 115118 / 第115000118期
        draw_no = ""
        m_no = re.search(r"(?:第\s*)?(\d{5,9})\s*期", block)
        if m_no:
            draw_no = m_no.group(1)
        else:
            # 退而求其次：找接近遊戲名稱後面的長數字
            candidates = [x for x in re.findall(r"(?<!\d)(\d{5,9})(?!\d)", block) if not re.match(r"20\d{6}", x)]
            if candidates:
                draw_no = candidates[0]
        # 號碼：只抓中獎號碼附近的數字，避免金額和電話
        local = block
        key_pos = -1
        for key in ["獎號", "中獎號碼", "開出順序", "大小順序", "第一區", "第二區"]:
            p = block.find(key)
            if p >= 0:
                key_pos = p
                break
        if key_pos >= 0:
            local = block[key_pos:key_pos+400]
        nums = extract_numbers(local, 7 if game in ["lotto", "power"] else 5)
        # 過濾日期/期別混入：如果抓到大於遊戲範圍的會自動排除，但 5,14 可能混入；因此至少需數量足夠
        need = 5 if game == "539" else 6
        if len(nums) >= need:
            special = None
            if game in ["lotto", "power"] and len(nums) >= 7:
                special = nums[6]
            rows.append(row_from_values(game, draw_no, d, nums, special, source))
    return dedupe_rows(rows)


def parse_html_tables(html: str, source: str) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    try:
        tables = pd.read_html(io.StringIO(html))
    except Exception:
        return []
    for t in tables:
        if t.empty:
            continue
        # Flatten columns
        t.columns = [" ".join([str(x) for x in col if str(x) != "nan"]) if isinstance(col, tuple) else str(col) for col in t.columns]
        for _, r in t.iterrows():
            line = " ".join([str(x) for x in r.tolist() if str(x) != "nan"])
            line = normalize_digits(line)
            game = infer_game_from_text(line)
            if not game:
                continue
            d = roc_to_ad_date(line) or ""
            m_no = re.search(r"(?:第\s*)?(\d{5,9})\s*期", line)
            draw_no = m_no.group(1) if m_no else ""
            nums = extract_numbers(line, 7 if game in ["lotto", "power"] else 5)
            need = 5 if game == "539" else 6
            if len(nums) >= need:
                special = nums[6] if game in ["lotto", "power"] and len(nums) >= 7 else None
                rows.append(row_from_values(game, draw_no, d, nums, special, source))
    return dedupe_rows(rows)


def dedupe_rows(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    out = []
    seen = set()
    for r in rows:
        key = (r.get("game"), r.get("draw_no") or r.get("draw_date"), tuple(r.get(f"n{i}", "") for i in range(1, 7)), r.get("special", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def fetch_latest_recent_rows() -> Tuple[List[Dict[str, str]], List[str]]:
    all_rows: List[Dict[str, str]] = []
    notes: List[str] = []
    for key in ["today_last_number", "latest_result", "result_query", "ctbc_result_all"]:
        url = OFFICIAL_URLS[key]
        html = safe_get(url)
        if not html:
            notes.append(f"{key}: 抓取失敗")
            continue
        (RAW_DIR / f"{key}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html").write_text(html, encoding="utf-8")
        rows = []
        rows.extend(parse_html_tables(html, key))
        rows.extend(parse_text_blocks_for_latest(html, key))
        rows = dedupe_rows(rows)
        all_rows.extend(rows)
        notes.append(f"{key}: 解析 {len(rows)} 筆")
    return dedupe_rows(all_rows), notes


def parse_history_download_links(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if any(ext in href.lower() for ext in [".zip", ".csv", ".xlsx", ".xls"]):
            if href.startswith("http"):
                links.append(href)
            else:
                links.append(requests.compat.urljoin(OFFICIAL_URLS["history_download"], href))
    return list(dict.fromkeys(links))


def normalize_history_columns(df: pd.DataFrame, source: str) -> List[Dict[str, str]]:
    rows = []
    # 常見欄位名：遊戲名稱,期別,開獎日期,獎號1...
    df.columns = [str(c).strip() for c in df.columns]
    for _, r in df.iterrows():
        vals = {str(k): str(v) for k, v in r.to_dict().items() if str(v) != "nan"}
        line = " ".join(vals.values())
        game = infer_game_from_text(line)
        if not game:
            continue
        draw_no = ""
        for c in df.columns:
            if "期" in c:
                draw_no = normalize_digits(vals.get(c, "")).strip()
                break
        draw_date = ""
        for c in df.columns:
            if "日期" in c:
                draw_date = roc_to_ad_date(vals.get(c, "")) or ""
                break
        nums = []
        special = None
        # 優先取獎號欄位
        for i in range(1, 7):
            found = None
            for c in df.columns:
                if f"獎號{i}" in c or f"獎號 {i}" in c or f"第{i}" in c:
                    found = vals.get(c, "")
                    break
            if found is not None:
                m = re.search(r"\d{1,2}", normalize_digits(found))
                if m:
                    nums.append(int(m.group()))
        for c in df.columns:
            if "特別" in c or "第二區" in c:
                m = re.search(r"\d{1,2}", normalize_digits(vals.get(c, "")))
                if m:
                    special = int(m.group())
                    break
        if len(nums) < (5 if game == "539" else 6):
            nums = extract_numbers(line, 7 if game in ["lotto", "power"] else 5)
            if game in ["lotto", "power"] and len(nums) >= 7 and special is None:
                special = nums[6]
        if len(nums) >= (5 if game == "539" else 6):
            rows.append(row_from_values(game, draw_no, draw_date, nums, special, source))
    return rows


def safe_get_bytes(url: str, timeout: int = 35) -> Optional[bytes]:
    last_err = None
    for verify in (True, False):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout, verify=verify)
            r.raise_for_status()
            if not verify:
                log(f"[SSL fallback OK] {url}")
            return r.content
        except requests.exceptions.SSLError as e:
            last_err = e
            if verify:
                log(f"[SSL fallback] 一般驗證失敗，改用備援抓取: {url}")
                continue
            log(f"抓取失敗: {url} | {e}")
            return None
        except Exception as e:
            last_err = e
            log(f"抓取失敗: {url} | {e}")
            return None
    if last_err:
        log(f"抓取失敗: {url} | {last_err}")
    return None


def update_from_history_download(max_files: int = 2) -> Tuple[List[Dict[str, str]], List[str]]:
    """補歷史用，抓最新幾個官方下載檔。不是每日即時核心。"""
    rows: List[Dict[str, str]] = []
    notes: List[str] = []
    html = safe_get(OFFICIAL_URLS["history_download"])
    if not html:
        return [], ["history_download: 抓取失敗"]
    links = parse_history_download_links(html)
    if not links:
        return [], ["history_download: 找不到下載連結，可能官方頁面改版"]
    # 只抓最新年度附近，避免太慢
    links = links[:max_files]
    for url in links:
        try:
            raw = safe_get_bytes(url, timeout=35)
            if raw is None:
                notes.append(f"history_download {url}: 抓取失敗")
                continue
            name = re.sub(r"[^0-9A-Za-z_.-]+", "_", url.split("/")[-1] or "download")
            (RAW_DIR / name).write_bytes(raw)
            parsed = 0
            if name.lower().endswith(".zip"):
                with zipfile.ZipFile(io.BytesIO(raw)) as z:
                    for info in z.infolist():
                        if not info.filename.lower().endswith((".csv", ".txt")):
                            continue
                        data = z.read(info.filename)
                        for enc in ["utf-8-sig", "utf-8", "cp950", "big5"]:
                            try:
                                txt = data.decode(enc)
                                df = pd.read_csv(io.StringIO(txt))
                                got = normalize_history_columns(df, f"history:{info.filename}")
                                rows.extend(got); parsed += len(got)
                                break
                            except Exception:
                                continue
            elif name.lower().endswith(".csv"):
                for enc in ["utf-8-sig", "utf-8", "cp950", "big5"]:
                    try:
                        df = pd.read_csv(io.BytesIO(raw), encoding=enc)
                        got = normalize_history_columns(df, f"history:{name}")
                        rows.extend(got); parsed += len(got)
                        break
                    except Exception:
                        continue
            notes.append(f"history_download {name}: 解析 {parsed} 筆")
        except Exception as e:
            notes.append(f"history_download {url}: 失敗 {e}")
    return dedupe_rows(rows), notes


def merge_updates(rows: List[Dict[str, str]]) -> Dict[str, Dict[str, int]]:
    result: Dict[str, Dict[str, int]] = {}
    for game in GAME_FILES:
        old = read_game_csv(game)
        new_rows = [r for r in rows if r.get("game") == game]
        new = pd.DataFrame(new_rows) if new_rows else empty_df()
        before = len(old)
        merged = pd.concat([old, new], ignore_index=True) if not new.empty else old
        merged = clean_df(merged)
        after = len(merged)
        save_game_csv(game, merged)
        result[game] = {"before": before, "after": after, "added": max(0, after - before), "fetched": len(new_rows)}
    return result


def latest_info(game: str) -> Dict[str, str]:
    df = read_game_csv(game)
    df = clean_df(df)
    if df.empty:
        return {"game": game, "draw_no": "", "draw_date": "", "numbers": "", "special": "", "rows": "0"}
    r = df.iloc[-1]
    max_i = 5 if game == "539" else 6
    nums = []
    for i in range(1, max_i + 1):
        v = valid_int_text(r.get(f"n{i}", ""), 1, 39 if game == "539" else 49)
        if v:
            nums.append(v)
    sp = ""
    if game == "lotto":
        sp = valid_int_text(r.get("special", ""), 1, 49)
    elif game == "power":
        sp = valid_int_text(r.get("special", ""), 1, 8)
    return {
        "game": game,
        "draw_no": clean_cell(r.get("draw_no", "")),
        "draw_date": clean_cell(r.get("draw_date", "")),
        "numbers": " ".join(nums),
        "special": sp,
        "rows": str(len(df)),
    }


def number_stats(game: str, window: int = 60) -> pd.DataFrame:
    df = clean_df(read_game_csv(game))
    if df.empty:
        return pd.DataFrame(columns=["number", "count", "last_seen_gap", "score"])
    df = df.tail(window).reset_index(drop=True)
    max_num = 39 if game == "539" else 49
    rows = []
    for n in range(1, max_num + 1):
        count = 0
        last_idx = None
        for idx, r in df.iterrows():
            nums = [int(r.get(f"n{i}")) for i in range(1, 7) if str(r.get(f"n{i}", "")).isdigit()]
            if n in nums:
                count += 1
                last_idx = idx
        gap = len(df) - 1 - last_idx if last_idx is not None else len(df)
        # 簡單分數：熱度 + 遺漏補償，不宣稱必中
        score = count * 1.0 + min(gap, 30) * 0.08
        rows.append({"number": n, "count": count, "last_seen_gap": gap, "score": round(score, 3)})
    return pd.DataFrame(rows).sort_values(["score", "count"], ascending=False)



def get_game_numbers_df(game: str) -> pd.DataFrame:
    """Return cleaned draw rows with list[int] numbers for scoring."""
    df = clean_df(read_game_csv(game))
    if df.empty:
        return df
    nums_col = []
    for _, r in df.iterrows():
        nums = []
        max_i = 5 if game == "539" else 6
        for i in range(1, max_i + 1):
            v = str(r.get(f"n{i}", "")).strip()
            if v.isdigit():
                nums.append(int(v))
        nums_col.append(nums)
    df = df.copy()
    df["_nums"] = nums_col
    return df[df["_nums"].map(len) >= (5 if game == "539" else 6)].reset_index(drop=True)


def build_539_number_confidence_rank(source_df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """Build a 539 single-number confidence table.

    This table ranks individual numbers 01-39, not 5-number combinations.
    It deliberately avoids flat placeholder scores: even when cloud history is
    still small, ties are split with deterministic, explainable factors
    (recent frequency, overdue gap, under-filled zone/parity, and a tiny stable
    tie-breaker). It is a ranking aid, not a winning guarantee.
    """
    df = source_df if source_df is not None else get_game_numbers_df("539")
    if df is None:
        df = pd.DataFrame()
    if not df.empty and "_nums" not in df.columns:
        try:
            df = get_game_numbers_df("539")
        except Exception:
            df = pd.DataFrame()

    cols = [
        "rank", "number", "confidence_score", "type",
        "recent_30_count", "recent_80_count", "gap", "reason",
        "hot_score", "gap_score", "zone_score", "tie_score",
    ]

    def _write(rows: List[Dict[str, object]], sample_total: int = 0) -> pd.DataFrame:
        out = pd.DataFrame(rows)
        if out.empty:
            out = pd.DataFrame(columns=cols)
        # 先用模型原始分數排序，再把畫面上的「信心分數」轉成排名強度。
        # 這樣不會因為小樣本上限把前幾名全部壓成同一個 76。
        sort_col = "_raw_score" if "_raw_score" in out.columns else "confidence_score"
        out["_sort_score"] = pd.to_numeric(out.get(sort_col, 0), errors="coerce").fillna(0)
        out["_recent30_sort"] = pd.to_numeric(out.get("recent_30_count", 0), errors="coerce").fillna(0)
        out["_gap_sort"] = pd.to_numeric(out.get("gap", 0), errors="coerce").fillna(0)
        out = out.sort_values(["_sort_score", "_recent30_sort", "_gap_sort", "number"], ascending=[False, False, False, True]).reset_index(drop=True)
        out["rank"] = range(1, len(out) + 1)

        # 顯示分數：不是中獎機率，是 Top20 排名強度。
        # 小樣本也要能看出差距，Top5 會落在 90+，後面逐步下降。
        if sample_total < 10:
            top_score, step = 96.0, 1.15
        elif sample_total < 30:
            top_score, step = 95.0, 1.05
        elif sample_total < 80:
            top_score, step = 94.0, 0.95
        else:
            top_score, step = 93.0, 0.85
        display_scores = []
        for i, (_, r) in enumerate(out.iterrows(), start=1):
            raw = float(r.get("_sort_score", 0) or 0)
            tiny = (raw % 1.0) * 0.18  # 保留一點模型差異，避免整數階梯太死。
            display_scores.append(round(max(39.0, min(99.0, top_score - (i - 1) * step + tiny)), 2))
        if display_scores:
            out["confidence_score"] = display_scores

        for c in cols:
            if c not in out.columns:
                out[c] = ""
        out = out[cols]
        out.to_csv(OUTPUT_DIR / "539_number_confidence_rank.csv", index=False, encoding="utf-8-sig")
        # compatible short file for older links
        out[["rank", "number", "confidence_score", "type"]].head(20).to_csv(OUTPUT_DIR / "539_number_rank.csv", index=False, encoding="utf-8-sig")
        return out

    starter_order = [
        4, 7, 13, 16, 22, 28, 33, 11, 19, 25, 31, 36, 2,
        5, 9, 14, 18, 23, 27, 32, 37, 1, 6, 10, 15, 20,
        24, 29, 34, 38, 3, 8, 12, 17, 21, 26, 30, 35, 39,
    ]

    if df.empty:
        rows = []
        for idx, n in enumerate(starter_order):
            zone = (n - 1) // 10 + 1
            # Deterministic starter ranking strength, not random and not flat 50.
            # Top5 會顯示 90+，但這是「推薦強度」不是中獎機率。
            score = 96.0 - idx * 1.15 + ((n * 11) % 7) * 0.03
            rows.append({
                "rank": 0,
                "number": f"{n:02d}",
                "confidence_score": round(max(39.0, score), 2),
                "_raw_score": round(max(39.0, score), 4),
                "type": "啟動分散",
                "recent_30_count": 0,
                "recent_80_count": 0,
                "gap": 0,
                "reason": f"尚未累積 539 歷史資料；啟動盤先用第 {zone} 區分散推薦。分數是排行強度，不是中獎機率",
                "hot_score": 0,
                "gap_score": 0,
                "zone_score": round(70 - idx * 0.6, 2),
                "tie_score": round(((n * 37) % 23) / 23 * 6, 2),
            })
        return _write(rows, sample_total=0)

    draws = [list(map(int, nums)) for nums in df.get("_nums", []) if isinstance(nums, list) and len(nums) >= 5]
    total = len(draws)
    if total <= 0:
        return build_539_number_confidence_rank(pd.DataFrame())

    recent30 = draws[-min(30, total):]
    recent80 = draws[-min(80, total):]
    all_recent = draws[-min(180, total):]

    def _counts(draw_list: List[List[int]]) -> Dict[int, int]:
        d = {n: 0 for n in range(1, 40)}
        for nums in draw_list:
            for n in nums[:5]:
                if 1 <= int(n) <= 39:
                    d[int(n)] += 1
        return d

    c30 = _counts(recent30)
    c80 = _counts(recent80)
    c180 = _counts(all_recent)

    # zone/parity pressure: zones/parities that appeared less than expected get a补位 bonus.
    zone_sizes = {0: 9, 1: 10, 2: 10, 3: 10}
    zone_hits = {z: 0 for z in zone_sizes}
    parity_hits = {0: 0, 1: 0}
    for nums in recent80:
        for n in nums[:5]:
            zone_hits[(int(n) - 1) // 10] += 1
            parity_hits[int(n) % 2] += 1

    total_hits = max(1, len(recent80) * 5)
    expected_by_zone = {z: total_hits * (size / 39) for z, size in zone_sizes.items()}
    expected_by_parity = {0: total_hits * (19 / 39), 1: total_hits * (20 / 39)}

    rows = []
    for n in range(1, 40):
        # last seen gap: 0 means latest draw, total means not seen in current cloud history.
        last_seen = None
        for idx in range(total - 1, -1, -1):
            if n in draws[idx]:
                last_seen = idx
                break
        gap_raw = total if last_seen is None else total - 1 - last_seen

        exp30 = max(0.001, len(recent30) * 5 / 39)
        exp80 = max(0.001, len(recent80) * 5 / 39)
        exp180 = max(0.001, len(all_recent) * 5 / 39)

        # Frequency score: equal to expectation is around 50, capped at 100.
        hot_score = min(100.0, (c30[n] / exp30) * 50.0)
        mid_score = min(100.0, (c80[n] / exp80) * 50.0)
        long_score = min(100.0, (c180[n] / exp180) * 50.0)

        # Gap score prefers moderate overdue, not immediate repeats and not absurdly old.
        ideal_gap = max(3, min(14, round(39 / 5)))
        gap_score = max(0.0, 100.0 - abs(gap_raw - ideal_gap) * 8.0)
        if gap_raw == 0:
            gap_score *= 0.62

        z = (n - 1) // 10
        z_expected = expected_by_zone[z]
        zone_under = max(0.0, (z_expected - zone_hits[z]) / max(0.001, z_expected))
        zone_score = min(100.0, zone_under * 100.0 + 38.0)

        parity = n % 2
        p_expected = expected_by_parity[parity]
        parity_under = max(0.0, (p_expected - parity_hits[parity]) / max(0.001, p_expected))
        parity_score = min(100.0, parity_under * 100.0 + 42.0)

        # Tiny deterministic tie-breaker so low-sample clouds do not show many identical 50s.
        tie_score = ((n * 37 + total * 13) % 23) / 23 * 100.0

        raw_score = (
            18.0
            + hot_score * 0.29
            + mid_score * 0.16
            + long_score * 0.08
            + gap_score * 0.20
            + zone_score * 0.13
            + parity_score * 0.06
            + tie_score * 0.08
        )
        # 原始分數只負責排序；顯示用分數在 _write() 依名次重新拉開，
        # 避免小樣本時一堆號碼一起卡在 76.0。
        score = max(1.0, raw_score)

        if c30[n] > exp30 * 1.25 and gap_raw >= ideal_gap:
            typ = "熱+補"
        elif c30[n] > exp30 * 1.25:
            typ = "熱號"
        elif gap_raw >= ideal_gap:
            typ = "補位"
        elif zone_score >= 70:
            typ = "區間補位"
        else:
            typ = "觀察"
        if total < 10:
            typ = "啟動-" + typ

        zone_name = f"{z * 10 + 1:02d}-{min(z * 10 + 10, 39):02d}"
        reason = (
            f"近{len(recent30)}期出現 {c30[n]} 次，近{len(recent80)}期出現 {c80[n]} 次；"
            f"遺漏 {gap_raw} 期；{zone_name} 區補位分 {zone_score:.1f}。"
        )
        if total < 10:
            reason += f"目前雲端樣本只有 {total} 期，畫面分數已改成排名強度刻度，Top5 可呈現 90+；不是中獎機率。"

        rows.append({
            "rank": 0,
            "number": f"{n:02d}",
            "confidence_score": round(score, 2),
            "_raw_score": round(raw_score, 4),
            "type": typ,
            "recent_30_count": c30[n],
            "recent_80_count": c80[n],
            "gap": gap_raw,
            "reason": reason,
            "hot_score": round(hot_score, 2),
            "gap_score": round(gap_score, 2),
            "zone_score": round(zone_score, 2),
            "tie_score": round(tie_score, 2),
        })
    return _write(rows, sample_total=total)


def build_539_confidence_rank_fallback(top_n: int = 20, source_df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """Generate a non-empty 539 ranking even when cloud history is still small.

    This prevents the dashboard from showing a blank Top10 right after a fresh
    cloud deployment. Scores are deliberately marked as 「啟動」 because the data
    volume is not enough for a full confidence model yet.
    """
    from itertools import combinations

    cols = ["rank", "numbers", "confidence_score", "confidence_level", "hot_score", "gap_score", "pair_score", "balance_score", "reason"]
    df = source_df if source_df is not None else get_game_numbers_df("539")
    if df is None or df.empty:
        # No data at all: still show balanced starter combinations so the UI
        # never looks broken. These are neutral placeholders, not predictions.
        seen_counts = {n: 0 for n in range(1, 40)}
        total = 0
        neutral_combos = [
            (1, 8, 16, 24, 32), (2, 9, 17, 25, 33), (3, 10, 18, 26, 34),
            (4, 11, 19, 27, 35), (5, 12, 20, 28, 36), (6, 13, 21, 29, 37),
            (7, 14, 22, 30, 38), (8, 15, 23, 31, 39), (1, 12, 18, 29, 35),
            (2, 11, 20, 27, 38), (3, 14, 21, 30, 36), (4, 9, 22, 28, 39),
            (5, 16, 23, 31, 37), (6, 10, 19, 25, 34), (7, 13, 17, 26, 33),
            (1, 15, 21, 28, 39), (2, 8, 19, 30, 37), (3, 11, 22, 29, 38),
            (4, 12, 23, 26, 35), (5, 14, 18, 31, 36),
        ]
        rows = []
        for combo in neutral_combos[:top_n]:
            balance_score = 75.0
            rows.append({
                "numbers": " ".join(f"{n:02d}" for n in combo),
                "confidence_score": 50.0,
                "confidence_level": "啟動",
                "hot_score": 0,
                "gap_score": 50,
                "pair_score": 0,
                "balance_score": balance_score,
                "reason": "尚未累積 539 資料，先顯示分散啟動組合",
            })
        out = pd.DataFrame(rows)
        out.insert(0, "rank", range(1, len(out) + 1))
        build_539_number_confidence_rank(source_df=df)
        return out[cols]
    else:
        total = len(df)
        seen_counts = {n: 0 for n in range(1, 40)}
        for nums in df.get("_nums", []):
            for n in nums:
                if 1 <= int(n) <= 39:
                    seen_counts[int(n)] += 1
        hot = sorted(seen_counts, key=lambda n: (seen_counts[n], -n), reverse=True)[:14]
        gap_like = [n for n in range(1, 40) if seen_counts[n] == 0][:10]
        base_pool = sorted(set(hot + gap_like))
        if len(base_pool) < 14:
            base_pool = sorted(set(base_pool + list(range(1, 40))))[:20]

    def balance(combo: Tuple[int, ...]) -> float:
        span = max(combo) - min(combo)
        odd = sum(1 for n in combo if n % 2)
        zone_hits = len(set((n - 1) // 10 for n in combo))
        span_score = min(span / 28 * 100, 100)
        odd_score = max(0, 100 - abs(odd - 2.5) * 18)
        zone_score = min(zone_hits / 4 * 100, 100)
        return span_score * 0.45 + odd_score * 0.25 + zone_score * 0.30

    rows = []
    for combo in combinations(base_pool[:22], 5):
        combo = tuple(sorted(combo))
        hot_score = sum(seen_counts.get(n, 0) for n in combo) / max(1, total) * 20 if total else 0
        gap_score = sum(1 for n in combo if seen_counts.get(n, 0) == 0) / 5 * 100 if total else 50
        pair_score = 0
        balance_score = balance(combo)
        score = hot_score * 0.35 + gap_score * 0.20 + balance_score * 0.45
        rows.append({
            "numbers": " ".join(f"{n:02d}" for n in combo),
            "confidence_score": round(score, 2),
            "confidence_level": "啟動",
            "hot_score": round(hot_score, 2),
            "gap_score": round(gap_score, 2),
            "pair_score": round(pair_score, 2),
            "balance_score": round(balance_score, 2),
            "reason": f"雲端資料目前 {total} 筆，先用現有熱度＋分散度產生啟動排行",
        })
    out = pd.DataFrame(rows).sort_values(["confidence_score", "balance_score", "numbers"], ascending=[False, False, True]).head(top_n).reset_index(drop=True)
    if out.empty:
        out = pd.DataFrame([{ 
            "numbers": "01 08 16 24 32", "confidence_score": 50.0, "confidence_level": "啟動",
            "hot_score": 0, "gap_score": 50, "pair_score": 0, "balance_score": 75,
            "reason": "尚未累積資料，先顯示啟動組合"
        }])
    out.insert(0, "rank", range(1, len(out) + 1))
    out = out[cols]

    # Also write the individual number ranking so linked output files are not empty.
    build_539_number_confidence_rank(source_df=df)
    return out


def build_539_confidence_rank(top_n: int = 20, window_short: int = 30, window_mid: int = 80, window_long: int = 180) -> pd.DataFrame:
    """Build 539 confidence ranking.

    This is a statistical confidence score, not a winning guarantee. It blends:
    - recent frequency from last 30 / 80 / 180 draws
    - overdue gap bonus
    - pair co-occurrence strength
    - mild balance penalty for too-clustered numbers
    """
    game = "539"
    df = get_game_numbers_df(game)
    cols = ["rank", "numbers", "confidence_score", "confidence_level", "hot_score", "gap_score", "pair_score", "balance_score", "reason"]
    if df.empty or len(df) < 10:
        out = build_539_confidence_rank_fallback(top_n=top_n, source_df=df)
        out.to_csv(OUTPUT_DIR / "539_confidence_rank.csv", index=False, encoding="utf-8-sig")
        return out

    max_num = 39
    total = len(df)
    windows = {
        "short": df.tail(min(window_short, total)),
        "mid": df.tail(min(window_mid, total)),
        "long": df.tail(min(window_long, total)),
    }

    def count_in(draws: pd.DataFrame, n: int) -> int:
        return sum(1 for nums in draws["_nums"] if n in nums)

    # individual score
    indiv = {}
    for n in range(1, max_num + 1):
        c_s = count_in(windows["short"], n)
        c_m = count_in(windows["mid"], n)
        c_l = count_in(windows["long"], n)
        last_seen = None
        for idx in range(total - 1, -1, -1):
            if n in df.loc[idx, "_nums"]:
                last_seen = idx
                break
        gap = total if last_seen is None else total - 1 - last_seen
        # normalize roughly to 0-100-ish
        hot = (c_s / max(1, len(windows["short"])) * 100 * 0.50 +
               c_m / max(1, len(windows["mid"])) * 100 * 0.30 +
               c_l / max(1, len(windows["long"])) * 100 * 0.20)
        # 539 theoretical average gap is about 7 draws; too overdue gets diminishing bonus
        gap_bonus = min(gap, 18) / 18 * 18
        indiv[n] = {"hot": hot, "gap": gap_bonus, "gap_raw": gap, "c_s": c_s, "c_m": c_m, "c_l": c_l,
                    "score": hot * 0.78 + gap_bonus * 0.22}

    # pair co-occurrence in recent/mid window
    from itertools import combinations
    pair_counts = {}
    for nums in windows["mid"]["_nums"]:
        for a, b in combinations(sorted(nums), 2):
            pair_counts[(a, b)] = pair_counts.get((a, b), 0) + 1
    max_pair = max(pair_counts.values()) if pair_counts else 1

    # candidate pool: best individual scores + top picks + some overdue/hot mix
    sorted_indiv = sorted(indiv, key=lambda x: indiv[x]["score"], reverse=True)
    pool = sorted(set(sorted_indiv[:18] + sorted(indiv, key=lambda x: indiv[x]["gap_raw"], reverse=True)[:8]))
    if len(pool) < 10:
        pool = list(range(1, max_num + 1))

    rows = []
    for combo in combinations(pool, 5):
        combo = tuple(sorted(combo))
        hot_score = sum(indiv[n]["hot"] for n in combo) / 5
        gap_score = sum(indiv[n]["gap"] for n in combo) / 5
        ps = []
        for a, b in combinations(combo, 2):
            ps.append(pair_counts.get((a, b), 0) / max_pair * 100)
        pair_score = sum(ps) / len(ps) if ps else 0
        # balance: avoid all small/all big, avoid too tight cluster, prefer odd/even mixed
        span = max(combo) - min(combo)
        odd = sum(1 for n in combo if n % 2)
        zone_hits = len(set((n - 1) // 10 for n in combo))
        span_score = min(span / 28 * 100, 100)
        odd_score = 100 - abs(odd - 2.5) * 18
        zone_score = min(zone_hits / 4 * 100, 100)
        balance_score = max(0, span_score * 0.45 + odd_score * 0.25 + zone_score * 0.30)
        score = hot_score * 0.42 + gap_score * 0.18 + pair_score * 0.22 + balance_score * 0.18
        if score >= 72:
            level = "高"
        elif score >= 62:
            level = "中高"
        elif score >= 52:
            level = "中"
        else:
            level = "觀察"
        top_hot = sorted(combo, key=lambda n: indiv[n]["hot"], reverse=True)[:2]
        top_gap = sorted(combo, key=lambda n: indiv[n]["gap_raw"], reverse=True)[:2]
        reason = f"熱號:{','.join(map(str, top_hot))}｜補位:{','.join(map(str, top_gap))}｜近{len(windows['mid'])}期組合關聯"
        rows.append({
            "numbers": " ".join(f"{n:02d}" for n in combo),
            "confidence_score": round(score, 2),
            "confidence_level": level,
            "hot_score": round(hot_score, 2),
            "gap_score": round(gap_score, 2),
            "pair_score": round(pair_score, 2),
            "balance_score": round(balance_score, 2),
            "reason": reason,
        })

    out = pd.DataFrame(rows).sort_values(["confidence_score", "hot_score", "pair_score"], ascending=False).head(top_n).reset_index(drop=True)
    out.insert(0, "rank", range(1, len(out) + 1))
    out = out[cols]
    out.to_csv(OUTPUT_DIR / "539_confidence_rank.csv", index=False, encoding="utf-8-sig")

    # individual confidence rank too
    build_539_number_confidence_rank(source_df=df)
    return out



def special_number_stats(game: str, window: int = 80) -> pd.DataFrame:
    """Rank special/second-zone numbers.

    lotto: special number 1-49
    power: second zone number 1-8
    539: no special number, returns empty.
    """
    if game == "539":
        return pd.DataFrame(columns=["number", "count", "last_seen_gap", "score"])
    df = clean_df(read_game_csv(game))
    if df.empty or "special" not in df.columns:
        return pd.DataFrame(columns=["number", "count", "last_seen_gap", "score"])
    df = df.tail(window).reset_index(drop=True)
    max_num = 8 if game == "power" else 49
    rows = []
    for n in range(1, max_num + 1):
        count = 0
        last_idx = None
        for idx, r in df.iterrows():
            v = str(r.get("special", "")).strip()
            if v.isdigit() and int(v) == n:
                count += 1
                last_idx = idx
        gap = len(df) - 1 - last_idx if last_idx is not None else len(df)
        score = count * 1.0 + min(gap, 30) * 0.08
        rows.append({"number": n, "count": count, "last_seen_gap": gap, "score": round(score, 3)})
    return pd.DataFrame(rows).sort_values(["score", "count"], ascending=False)


def make_today_picks() -> Dict[str, Dict[str, List[int]]]:
    """Build pick suggestions with correct zones/special labels.

    Returns:
      539:   {main:[5 numbers], special:[]}
      lotto: {main:[6 numbers], special:[1 special number]}
      power: {main:[6 first-zone numbers], special:[1 second-zone number]}
    """
    picks: Dict[str, Dict[str, List[int]]] = {}
    for game in GAME_FILES:
        stat = number_stats(game, 60)
        k = 5 if game == "539" else 6
        main_nums = stat.head(k)["number"].astype(int).tolist() if not stat.empty else []
        special_nums: List[int] = []
        sp_stat = special_number_stats(game, 80)
        if game in ["lotto", "power"] and not sp_stat.empty:
            special_nums = sp_stat.head(1)["number"].astype(int).tolist()
            sp_stat.to_csv(OUTPUT_DIR / f"{game}_special_rank.csv", index=False, encoding="utf-8-sig")
        picks[game] = {"main": main_nums, "special": special_nums}
        stat.to_csv(OUTPUT_DIR / f"{game}_number_rank.csv", index=False, encoding="utf-8-sig")

    # 539 信心排行：輸出組合排行與單號排行
    build_539_confidence_rank(top_n=20)

    with (OUTPUT_DIR / "lottery_today_picks.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["game", "main_numbers", "special_or_second_zone", "display", "note"])
        for g, obj in picks.items():
            main = " ".join(map(str, obj.get("main", [])))
            sp = " ".join(map(str, obj.get("special", [])))
            if g == "539":
                display = f"539：{main}｜無特別號"
            elif g == "lotto":
                display = f"大樂透：{main}｜特別號：{sp or '資料不足'}"
            else:
                display = f"威力彩：第一區 {main}｜第二區 {sp or '資料不足'}"
            w.writerow([g, main, sp, display, "統計參考，不保證中獎"])
    return picks


def html_escape(s: object) -> str:
    import html
    return html.escape(str(s if s is not None else ""))


def build_dashboard(update_notes: List[str], merge_result: Dict[str, Dict[str, int]], picks: Dict[str, List[int]]) -> None:
    latest = [latest_info(g) for g in ["539", "lotto", "power"]]
    rows_html = "".join(
        f"<tr><td>{html_escape(r['game'])}</td><td>{html_escape(r['draw_no'])}</td><td>{html_escape(r['draw_date'])}</td>"
        f"<td>{html_escape(r['numbers'])}</td><td>{html_escape(r['special'])}</td><td>{html_escape(r['rows'])}</td></tr>"
        for r in latest
    )
    merge_html = "".join(
        f"<tr><td>{g}</td><td>{v['before']}</td><td>{v['fetched']}</td><td>{v['added']}</td><td>{v['after']}</td></tr>"
        for g, v in merge_result.items()
    )
    def pick_display(g: str, obj: object) -> Tuple[str, str]:
        if isinstance(obj, dict):
            main = " ".join(map(str, obj.get("main", [])))
            sp = " ".join(map(str, obj.get("special", [])))
        else:
            main = " ".join(map(str, obj or []))
            sp = ""
        if g == "539":
            return main, "無特別號"
        if g == "lotto":
            return main, f"特別號：{sp or '資料不足'}"
        if g == "power":
            return main, f"第二區：{sp or '資料不足'}"
        return main, sp

    picks_html = "".join(
        f"<tr><td>{g}</td><td>{pick_display(g, obj)[0]}</td><td>{pick_display(g, obj)[1]}</td><td>統計參考，不保證中獎</td></tr>"
        for g, obj in picks.items()
    )
    # 539 單號碼信心推薦 HTML（顯示各號碼，不顯示組合）
    number_conf_path = OUTPUT_DIR / "539_number_confidence_rank.csv"
    if not number_conf_path.exists():
        try:
            build_539_confidence_rank(top_n=20)
        except Exception:
            build_539_confidence_rank_fallback(top_n=20)
    try:
        num_conf_df = pd.read_csv(number_conf_path, dtype=str).head(20)
        if num_conf_df.empty:
            build_539_confidence_rank_fallback(top_n=20)
            num_conf_df = pd.read_csv(number_conf_path, dtype=str).head(20)
        _score_num = pd.to_numeric(num_conf_df.get("confidence_score", 0), errors="coerce").fillna(0)
        top5_nums = " ".join(num_conf_df.head(5)["number"].astype(str).map(lambda x: str(x).zfill(2)).tolist()) if _score_num.max() > 0 else ""
        def _num_reason(r: pd.Series) -> str:
            existing = clean_cell(r.get("reason", ""))
            if existing:
                return existing
            typ = clean_cell(r.get("type", "觀察")) or "觀察"
            c30 = clean_cell(r.get("recent_30_count", "0")) or "0"
            c80 = clean_cell(r.get("recent_80_count", "0")) or "0"
            gap = clean_cell(r.get("gap", "0")) or "0"
            return f"{typ}｜近30期出現 {c30} 次｜近80期出現 {c80} 次｜遺漏 {gap} 期"
        number_conf_rows = "".join(
            f"<tr><td>{html_escape(r.get('rank',''))}</td><td class='num-big'><b>{html_escape(str(r.get('number','')).zfill(2))}</b></td>"
            f"<td>{html_escape(r.get('confidence_score',''))}</td><td>{html_escape(r.get('type',''))}</td>"
            f"<td>{html_escape(r.get('recent_30_count',''))}</td><td>{html_escape(r.get('recent_80_count',''))}</td>"
            f"<td>{html_escape(r.get('gap',''))}</td><td>{html_escape(_num_reason(r))}</td></tr>"
            for _, r in num_conf_df.iterrows()
        )
    except Exception as exc:
        top5_nums = ""
        number_conf_rows = f"<tr><td colspan='8'>539 單號碼信心推薦讀取失敗：{html_escape(exc)}</td></tr>"
    notes_html = "".join(f"<li>{html_escape(n)}</li>" for n in update_notes)
    html = f"""<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>彩球 Auto Update Dashboard</title>
<style>
body{{margin:0;background:#eef2f6;color:#0b2540;font-family:'Microsoft JhengHei',Arial,sans-serif}}.wrap{{max-width:1180px;margin:auto;padding:18px}}.card{{background:#fffdf7;border:1px solid #dde4dc;border-radius:18px;margin:14px 0;padding:16px;box-shadow:0 8px 24px rgba(15,23,42,.06)}}h1{{margin:0 0 6px;font-size:28px}}h2{{font-size:20px}}.sub{{color:#64748b;font-size:13px;line-height:1.6}}table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{border-bottom:1px solid #e2e8d7;padding:9px;text-align:left;white-space:nowrap}}th{{background:#e8eee2}}.num-big b{{font-size:20px;letter-spacing:.08em;color:#b45309}}.good{{background:#ecfdf5;border:1px solid #99f6e4;color:#0f766e;border-radius:12px;padding:12px;font-weight:800}}.warn{{background:#fffbeb;border:1px solid #fde68a;color:#92400e;border-radius:12px;padding:12px;font-weight:800}}.tbl{{overflow:auto}}code{{background:#f1f5f9;padding:2px 5px;border-radius:6px}}.cloud-actions{{display:flex!important;gap:10px!important;align-items:center!important;flex-wrap:wrap!important;margin-top:14px!important}}.cloud-actions button,.cloud-actions a{{appearance:none!important;border:0!important;border-radius:999px!important;background:#fbbf24!important;color:#111827!important;font-weight:900!important;padding:12px 18px!important;text-decoration:none!important;cursor:pointer!important;font-size:15px!important}}.cloud-actions a{{background:#e5e7eb!important}}#page-update-status{{font-weight:800!important;color:#0f766e!important}}
</style></head><body><div class='wrap'>
<div class='card'><h1>彩球 Auto Update Dashboard</h1><div class='sub'>產生時間：{now_str()}｜每次跑會先抓最新資料、合併 CSV、去重複，再產生報告。</div><div class='cloud-actions'><button type='button' onclick="lotteryCloudUpdate('weekly')">更新資料</button><a href='/api/status' target='_blank' rel='noopener'>狀態</a><span id='page-update-status'>按「更新資料」即可重新抓取並產生報表</span></div></div>
<div class='card'><div class='good'>完成：已執行自動更新流程。若官方頁面尚未公布最新期別，下面會保留目前最新資料。</div></div>
<div class='card'><h2>最新資料狀態</h2><div class='tbl'><table><thead><tr><th>遊戲</th><th>最新期別</th><th>開獎日期</th><th>獎號</th><th>特別號 / 第二區</th><th>CSV筆數</th></tr></thead><tbody>{rows_html}</tbody></table></div></div>
<div class='card'><h2>本次更新合併結果</h2><div class='tbl'><table><thead><tr><th>遊戲</th><th>原本筆數</th><th>抓到筆數</th><th>新增筆數</th><th>合併後筆數</th></tr></thead><tbody>{merge_html}</tbody></table></div></div>
<div class='card'><h2>今日統計參考號碼</h2><div class='tbl'><table><thead><tr><th>遊戲</th><th>主號 / 第一區</th><th>特別號 / 第二區</th><th>說明</th></tr></thead><tbody>{picks_html}</tbody></table></div></div>
<div class='card'><h2>539 單號碼信心推薦 Top20</h2><div class='warn'>這裡是 01～39 各單號碼的信心推薦，不是 5 碼組合；分數會依熱度、遺漏、區間補位分開計算；90+ 代表排行強度高，不是中獎機率。統計分數不保證中獎。</div><div class='good'>目前 539 單號推薦 Top5：<b>{html_escape(top5_nums) if top5_nums else '資料不足'}</b></div><div class='tbl'><table><thead><tr><th>排名</th><th>號碼</th><th>信心分數</th><th>類型</th><th>近30期</th><th>近80期</th><th>遺漏期數</th><th>推薦理由</th></tr></thead><tbody>{number_conf_rows}</tbody></table></div></div>
<div class='card'><h2>更新來源紀錄</h2><ul>{notes_html}</ul></div>
<div class='card'><h2>輸出檔</h2><ul><li><code>data/lottery/539.csv</code></li><li><code>data/lottery/lotto.csv</code></li><li><code>data/lottery/power.csv</code></li><li><code>output/lottery_today_picks.csv</code></li><li><code>output/*_number_rank.csv</code></li><li><code>output/lotto_special_rank.csv</code></li><li><code>output/power_special_rank.csv</code></li><li><code>output/539_number_confidence_rank.csv</code>（539 單號碼信心推薦）</li><li><code>output/539_confidence_rank.csv</code>（組合備用輸出，不在首頁顯示）</li></ul></div>
</div><script>
(function(){{
  const statusEl = document.getElementById('page-update-status');
  async function pollAndReload(){{
    try{{
      const res = await fetch('/api/status', {{cache:'no-store'}});
      const data = await res.json();
      if(data.update && data.update.running){{
        if(statusEl) statusEl.textContent = '更新中…完成後會自動重新整理';
        setTimeout(pollAndReload, 3500);
        return;
      }}
      if(data.update && data.update.last_ok === true){{
        if(statusEl) statusEl.textContent = '更新完成，重新載入…';
        location.href='/?t=' + Date.now();
        return;
      }}
      if(data.update && data.update.last_ok === false){{
        if(statusEl) statusEl.textContent = '更新失敗，請查看狀態或錯誤頁';
      }}
    }}catch(e){{ if(statusEl) statusEl.textContent = '狀態讀取失敗'; }}
  }}
  window.lotteryCloudUpdate = async function(mode){{
    const buttons = document.querySelectorAll('.cloud-actions button,#lottery-cloud-toolbar button');
    buttons.forEach(b => b.disabled = true);
    if(statusEl) statusEl.textContent = '送出更新…';
    try{{
      const res = await fetch('/api/web-update?mode=' + encodeURIComponent(mode || 'weekly'), {{method:'POST', cache:'no-store'}});
      const text = await res.text();
      if(!res.ok){{
        if(statusEl) statusEl.textContent = '更新啟動失敗 ' + res.status;
        alert(text);
        buttons.forEach(b => b.disabled = false);
        return;
      }}
      if(statusEl) statusEl.textContent = '已開始更新…';
      setTimeout(pollAndReload, 2500);
    }}catch(e){{
      if(statusEl) statusEl.textContent = '更新啟動失敗';
      alert(String(e));
      buttons.forEach(b => b.disabled = false);
    }}
  }};
}})();
</script></body></html>"""
    (OUTPUT_DIR / "lottery_final_dashboard.html").write_text(html, encoding="utf-8")
    # status
    status = {
        "generated_at": now_str(),
        "latest": latest,
        "merge": merge_result,
        "notes": update_notes,
    }
    (OUTPUT_DIR / "lottery_auto_update_status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")


def main(mode: str = "daily", open_dashboard: bool = True) -> int:
    LOG_DIR.mkdir(exist_ok=True, parents=True)
    log("=" * 60)
    log(f"彩球自動更新開始 mode={mode}")
    notes: List[str] = []
    rows: List[Dict[str, str]] = []

    # 每日/每週都抓最新頁；weekly 額外嘗試抓歷史下載補檔
    recent_rows, recent_notes = fetch_latest_recent_rows()
    rows.extend(recent_rows)
    notes.extend(recent_notes)

    if mode.lower() in ["weekly", "full"]:
        hist_rows, hist_notes = update_from_history_download(max_files=2)
        rows.extend(hist_rows)
        notes.extend(hist_notes)

    rows = dedupe_rows(rows)
    merge_result = merge_updates(rows)
    picks = make_today_picks()
    build_dashboard(notes, merge_result, picks)

    log("更新完成")
    for g, r in merge_result.items():
        log(f"{g}: before={r['before']} fetched={r['fetched']} added={r['added']} after={r['after']}")
    dashboard = OUTPUT_DIR / "lottery_final_dashboard.html"
    print("\n=== 彩球更新完成 ===")
    print(f"Dashboard: {dashboard}")
    print("今日參考號碼：")
    for g, obj in picks.items():
        main = obj.get("main", []) if isinstance(obj, dict) else obj
        sp = obj.get("special", []) if isinstance(obj, dict) else []
        main_txt = " ".join(map(str, main)) if main else "資料不足"
        sp_txt = " ".join(map(str, sp)) if sp else "資料不足"
        if g == "539":
            print(f"  539: {main_txt}｜無特別號")
        elif g == "lotto":
            print(f"  大樂透: {main_txt}｜特別號: {sp_txt}")
        elif g == "power":
            print(f"  威力彩: 第一區 {main_txt}｜第二區: {sp_txt}")
        else:
            print(f"  {g}: {main_txt}")
    conf_path = OUTPUT_DIR / "539_confidence_rank.csv"
    if conf_path.exists():
        try:
            conf_df = pd.read_csv(conf_path).head(5)
            print("\n539 信心排行 Top5：")
            for _, r in conf_df.iterrows():
                print(f"  #{int(r['rank'])} {r['numbers']}  分數:{r['confidence_score']}  等級:{r['confidence_level']}")
        except Exception:
            pass
    if open_dashboard:
        try:
            webbrowser.open(dashboard.resolve().as_uri())
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    mode = "daily"
    open_dash = True
    args = [a.lower() for a in sys.argv[1:]]
    if "--weekly" in args:
        mode = "weekly"
    if "--full" in args:
        mode = "full"
    if "--no-open" in args:
        open_dash = False
    try:
        raise SystemExit(main(mode=mode, open_dashboard=open_dash))
    except Exception:
        err = traceback.format_exc()
        log(err)
        OUTPUT_DIR.mkdir(exist_ok=True, parents=True)
        (OUTPUT_DIR / "lottery_error.txt").write_text(err, encoding="utf-8")
        print(err)
        raise SystemExit(1)
