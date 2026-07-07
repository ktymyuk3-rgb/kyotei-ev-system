"""
競艇デイリー期待値スクリーナー
================================
その日の全開催場・全レースの出走表と3連単オッズを取得し、
推定確率 × オッズ = 期待値(回収率) が閾値を超える買い目だけを
HTMLレポートとCSVに出力します。

【重要な注意】
- 公式サイトへのアクセスは1リクエストあたり REQUEST_INTERVAL 秒(既定4秒)空けます。
  全場取得には30分前後かかりますが、これはサーバー負荷を避けるための仕様です。
  絶対に間隔を短くしないでください。
- オッズは締切まで変動します。このスクリプトが計算する期待値は
  「取得時点のオッズ」に基づくもので、購入時点では変わっています。
  レポートは候補の絞り込みに使い、購入前に必ず最新オッズで再確認してください。
- 確率モデルは簡易的なものです。特に人気薄のオッズは市場が過大評価する傾向
  (favorite-longshot bias)があり、モデル上の「期待値100%超」の多くは
  モデル誤差です。閾値は120%以上を推奨します。
- 個人の予想検討目的での利用を想定しています。

【必要ライブラリ】
    pip install requests beautifulsoup4 lxml --break-system-packages

【使い方】
    # 今日の全レースをスキャン(閾値120%)
    python kyotei_daily_screener.py

    # 日付・閾値・対象場を指定
    python kyotei_daily_screener.py --date 20260706 --threshold 1.3 --stadiums 12,22,24

【毎日自動実行(例)】
    Mac/Linux(cron): 毎朝9時に実行
        0 9 * * * cd /path/to/dir && python3 kyotei_daily_screener.py >> screener.log 2>&1
    Windows: タスクスケジューラで「プログラムの開始」に python.exe、
        引数に kyotei_daily_screener.py のフルパスを指定
"""

import argparse
import csv
import itertools
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BOATRACE_BASE = "https://www.boatrace.jp/owpc/pc/race"
REQUEST_INTERVAL = 4.0  # 秒。これより短くしないこと。

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

STADIUM_NAMES = {
    "01": "桐生", "02": "戸田", "03": "江戸川", "04": "平和島", "05": "多摩川",
    "06": "浜名湖", "07": "蒲郡", "08": "常滑", "09": "津", "10": "三国",
    "11": "びわこ", "12": "住之江", "13": "尼崎", "14": "鳴門", "15": "丸亀",
    "16": "児島", "17": "宮島", "18": "徳山", "19": "下関", "20": "若松",
    "21": "芦屋", "22": "福岡", "23": "唐津", "24": "大村",
}

# コース別1着率の全国平均(枠なり想定の事前分布)
COURSE_PRIOR = [0.553, 0.144, 0.122, 0.105, 0.056, 0.020]


# ────────────────────────── データ構造 ──────────────────────────

@dataclass
class BoatEntry:
    boat_number: int
    racer_name: str = ""
    zenkoku_win: float = 0.0
    touchi_win: float = 0.0
    motor_2: float = 0.0
    avg_st: float = 0.20


@dataclass
class RaceCandidate:
    stadium: str
    race_no: int
    combo: str          # 例 "1-3-2"
    prob: float         # モデル推定確率
    odds: float         # 取得時点の3連単オッズ
    ev: float           # prob * odds
    deadline_note: str = ""


# ────────────────────────── 通信 ──────────────────────────

class RateLimitedSession:
    def __init__(self, interval: float = REQUEST_INTERVAL):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.interval = interval
        self._last = 0.0

    def get(self, url: str):
        wait = self.interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        try:
            resp = self.session.get(url, timeout=15)
        except requests.exceptions.RequestException as e:
            print(f"  [警告] 通信エラー: {e}", file=sys.stderr)
            self._last = time.time()
            return None
        self._last = time.time()
        return resp


def _to_float(text, default=0.0):
    try:
        return float(str(text).strip().replace("-", "") or default)
    except (ValueError, AttributeError):
        return default


# ────────────────────────── スクレイピング ──────────────────────────

def scrape_racecard(sess, date_str, stadium, race_no):
    """出走表から6艇分の主要データを取得。失敗時は空リスト。"""
    url = f"{BOATRACE_BASE}/racelist?jcd={stadium}&hd={date_str}&rno={race_no}"
    resp = sess.get(url)
    if resp is None or resp.status_code != 200:
        return []
    soup = BeautifulSoup(resp.text, "lxml")
    rows = soup.select("tbody.is-fs12")
    if len(rows) < 6:
        return []  # 開催なし or 構造変化
    boats = []
    for i, row in enumerate(rows[:6], start=1):
        cells = row.select("td")
        e = BoatEntry(boat_number=i)
        try:
            # cells[1] は選手写真セル(サイト仕様変更で追加)のため、
            # 名前以降のデータ列は cells[2] から始まる。
            if len(cells) > 2:
                link = cells[2].find("a")
                if link:
                    e.racer_name = link.get_text(strip=True)
            if len(cells) > 3:
                st_parts = [p for p in cells[3].get_text("|", strip=True).split("|") if p]
                if st_parts:
                    e.avg_st = _to_float(st_parts[-1], 0.20)
            if len(cells) > 4:
                zk = [p for p in cells[4].get_text("|", strip=True).split("|") if p]
                if zk:
                    e.zenkoku_win = _to_float(zk[0])
            if len(cells) > 5:
                tc = [p for p in cells[5].get_text("|", strip=True).split("|") if p]
                if tc:
                    e.touchi_win = _to_float(tc[0])
            if len(cells) > 6:
                mt = [p for p in cells[6].get_text("|", strip=True).split("|") if p]
                if len(mt) >= 2:
                    e.motor_2 = _to_float(mt[1])
        except (IndexError, AttributeError):
            pass
        boats.append(e)
    return boats


def scrape_trifecta_odds(sess, date_str, stadium, race_no):
    """
    3連単オッズページから120通りのオッズを取得。
    公式のオッズ表: 6列(1着=1〜6号艇)、各列20行(2着5通り×3着4通り)。
    戻り値: {"1-2-3": 6.5, ...} 取得失敗時は空dict。
    """
    url = f"{BOATRACE_BASE}/odds3t?jcd={stadium}&hd={date_str}&rno={race_no}"
    resp = sess.get(url)
    if resp is None or resp.status_code != 200:
        return {}
    soup = BeautifulSoup(resp.text, "lxml")
    cells = soup.select("td.oddsPoint")
    if len(cells) != 120:
        print(f"  [警告] {stadium}-{race_no}R: オッズ表の構造が想定と異なります"
              f"(セル数{len(cells)})。スキップ。", file=sys.stderr)
        return {}

    odds_map = {}
    for col in range(6):          # 1着 = col+1
        first = col + 1
        seconds = [b for b in range(1, 7) if b != first]
        row = 0
        for second in seconds:
            thirds = [b for b in range(1, 7) if b not in (first, second)]
            for third in thirds:
                cell = cells[row * 6 + col]
                text = cell.get_text(strip=True)
                try:
                    odds_map[f"{first}-{second}-{third}"] = float(text)
                except ValueError:
                    pass  # 欠場・発売なし等
                row += 1
    return odds_map


def detect_active_stadiums(sess, date_str):
    """当日開催中の場を判定(各場の1Rの出走表が存在するかで判断)。"""
    active = []
    print("開催場を確認中(24場を順にチェック、約2分)...")
    for code in STADIUM_NAMES:
        boats = scrape_racecard(sess, date_str, code, 1)
        if boats:
            active.append(code)
            print(f"  {STADIUM_NAMES[code]}({code}) 開催あり")
    return active


# ────────────────────────── 確率モデル ──────────────────────────

def _zscores(arr):
    m = sum(arr) / len(arr)
    sd = math.sqrt(sum((v - m) ** 2 for v in arr) / len(arr)) or 1.0
    return [(v - m) / sd for v in arr]


def win_probs(boats):
    z_zen = _zscores([b.zenkoku_win for b in boats])
    z_tou = _zscores([b.touchi_win for b in boats])
    z_mot = _zscores([b.motor_2 for b in boats])
    z_st = _zscores([-b.avg_st for b in boats])
    skill = [0.38 * z_zen[i] + 0.18 * z_tou[i] + 0.26 * z_mot[i] + 0.18 * z_st[i]
             for i in range(6)]
    raw = [COURSE_PRIOR[i] * math.exp(1.15 * skill[i]) for i in range(6)]
    s = sum(raw)
    return [r / s for r in raw]


def trifecta_probs(p):
    """Plackett-Luce(ダンピング付き)で3連単120通りの確率を返す。"""
    probs = {}
    a, b = 0.92, 0.85
    for i, j, k in itertools.permutations(range(6), 3):
        rest1 = [x for x in range(6) if x != i]
        rest2 = [x for x in rest1 if x != j]
        s1 = sum(p[x] ** a for x in rest1)
        s2 = sum(p[x] ** b for x in rest2)
        probs[f"{i+1}-{j+1}-{k+1}"] = p[i] * (p[j] ** a / s1) * (p[k] ** b / s2)
    return probs


# ────────────────────────── レポート ──────────────────────────

def write_reports(candidates, date_str, threshold, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"ev_report_{date_str}.csv"
    html_path = out_dir / f"ev_report_{date_str}.html"

    candidates.sort(key=lambda c: c.ev, reverse=True)

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["場", "レース", "買い目", "推定確率%", "オッズ(取得時)", "回収率%"])
        for c in candidates:
            w.writerow([STADIUM_NAMES.get(c.stadium, c.stadium), f"{c.race_no}R", c.combo,
                        f"{c.prob*100:.2f}", f"{c.odds:.1f}", f"{c.ev*100:.1f}"])

    rows_html = "\n".join(
        f"<tr><td>{STADIUM_NAMES.get(c.stadium, c.stadium)}</td><td>{c.race_no}R</td>"
        f"<td class='combo'>{c.combo}</td><td>{c.prob*100:.2f}%</td>"
        f"<td>{c.odds:.1f}</td><td class='ev'>{c.ev*100:.1f}%</td></tr>"
        for c in candidates
    )
    html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>期待値レポート {date_str}</title>
<style>
body{{font-family:'Hiragino Kaku Gothic ProN',sans-serif;background:#0d1b31;color:#e8edf5;
padding:16px;max-width:640px;margin:0 auto}}
h1{{font-size:18px}} .meta{{color:#8fa3bf;font-size:12px;line-height:1.6}}
table{{width:100%;border-collapse:collapse;margin-top:14px;font-size:14px}}
th,td{{padding:8px 6px;border-bottom:1px solid rgba(120,160,210,.15);text-align:center}}
th{{color:#6fd3dd;font-size:11px;letter-spacing:.1em}}
.combo{{font-weight:800}} .ev{{color:#4ade80;font-weight:800}}
.warn{{background:rgba(60,40,20,.5);border:1px solid rgba(245,197,24,.3);border-radius:10px;
padding:10px 12px;font-size:11px;color:#d9c48a;line-height:1.7;margin-top:16px}}
</style></head><body>
<h1>競艇 期待値レポート {date_str}</h1>
<p class="meta">閾値: 回収率{threshold*100:.0f}%以上 / 抽出数: {len(candidates)}件<br>
オッズは取得時点のものです。締切までに大きく変動するため、購入前に必ず最新オッズで再計算してください。</p>
<table><tr><th>場</th><th>R</th><th>買い目</th><th>推定確率</th><th>オッズ</th><th>回収率</th></tr>
{rows_html}
</table>
<div class="warn">⚠ この期待値はモデル推定であり的中を保証しません。人気薄の「高期待値」の多くはモデル誤差です。
控除率25%の構造上、平均期待値はマイナスです。余剰資金の範囲で、1点あたりの投資は小さく。</div>
</body></html>"""
    html_path.write_text(html, encoding="utf-8")
    return csv_path, html_path


# ────────────────────────── メイン ──────────────────────────

def main():
    parser = argparse.ArgumentParser(description="競艇デイリー期待値スクリーナー")
    parser.add_argument("--date", default=datetime.now().strftime("%Y%m%d"),
                        help="対象日 YYYYMMDD(既定: 今日)")
    parser.add_argument("--threshold", type=float, default=1.2,
                        help="抽出する回収率の下限(既定1.2 = 120%%)")
    parser.add_argument("--stadiums", default="",
                        help="対象場コードをカンマ区切りで指定(例 12,22,24)。省略時は全開催場")
    parser.add_argument("--max-odds", type=float, default=100.0,
                        help="この倍率を超えるオッズは除外(モデル誤差対策、既定100倍)")
    parser.add_argument("--out", default=".", help="レポート出力先ディレクトリ")
    args = parser.parse_args()

    date_str = args.date
    sess = RateLimitedSession()

    if args.stadiums:
        stadiums = [s.strip().zfill(2) for s in args.stadiums.split(",")]
    else:
        stadiums = detect_active_stadiums(sess, date_str)

    if not stadiums:
        print("開催場が見つかりませんでした。日付を確認してください。")
        return

    n_req = len(stadiums) * 12 * 2
    print(f"\n対象: {len(stadiums)}場 × 12R = {len(stadiums)*12}レース")
    print(f"推定所要時間: 約{n_req * REQUEST_INTERVAL / 60:.0f}分"
          f"(アクセス間隔{REQUEST_INTERVAL}秒厳守のため)\n")

    candidates = []
    for stadium in stadiums:
        name = STADIUM_NAMES.get(stadium, stadium)
        for race_no in range(1, 13):
            boats = scrape_racecard(sess, date_str, stadium, race_no)
            if not boats:
                continue
            odds_map = scrape_trifecta_odds(sess, date_str, stadium, race_no)
            if not odds_map:
                continue
            p = win_probs(boats)
            tri = trifecta_probs(p)
            hits = 0
            for combo, prob in tri.items():
                odds = odds_map.get(combo)
                if odds is None or odds > args.max_odds:
                    continue
                ev = prob * odds
                if ev >= args.threshold:
                    candidates.append(RaceCandidate(stadium, race_no, combo, prob, odds, ev))
                    hits += 1
            print(f"{name} {race_no:2d}R 完了(抽出{hits}件)")

    if not candidates:
        print(f"\n回収率{args.threshold*100:.0f}%以上の買い目は見つかりませんでした。")
        print("(それが正常です。プラス期待値の買い目は毎日あるものではありません)")
        return

    csv_path, html_path = write_reports(candidates, date_str, args.threshold, args.out)
    print(f"\n抽出: {len(candidates)}件")
    print(f"CSV : {csv_path}")
    print(f"HTML: {html_path}")
    print("\n※オッズは取得時点のものです。購入前に必ず最新オッズを確認してください。")


if __name__ == "__main__":
    main()
