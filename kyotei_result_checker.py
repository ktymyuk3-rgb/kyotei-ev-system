"""
競艇 結果照合・収支トラッカー
================================
kyotei_daily_screener.py が出力した ev_report_YYYYMMDD.csv を読み込み、
公式サイトのレース結果と照合して的中/外れを判定、
累計収支(history.csv)とダッシュボード(dashboard.html)を更新します。

1点100円で買った想定のシミュレーション収支を記録します。
実際に購入していなくても「もし買っていたら」の検証(ペーパートレード)として使えます。
むしろ最初の1〜2ヶ月はペーパートレードでモデルの実力を確認することを強く推奨します。

【使い方】
    # 今日のレポートを結果と照合(全レース終了後の夜に実行)
    python kyotei_result_checker.py

    # 日付指定
    python kyotei_result_checker.py --date 20260706

【毎日自動実行(例)】
    cron: 毎晩22時(ナイター終了後)
        0 22 * * * cd /path/to/dir && python3 kyotei_result_checker.py >> checker.log 2>&1

【アクセスについて】
    結果取得は「抽出された買い目があるレースのみ」に限定されます。
    1レースあたり1リクエスト、間隔4秒厳守。
"""

import argparse
import csv
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

BOATRACE_BASE = "https://www.boatrace.jp/owpc/pc/race"
REQUEST_INTERVAL = 4.0
BET_UNIT = 100  # 1点あたりの想定購入額(円)

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
NAME_TO_CODE = {v: k for k, v in STADIUM_NAMES.items()}


class RateLimitedSession:
    def __init__(self, interval=REQUEST_INTERVAL):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.interval = interval
        self._last = 0.0

    def get(self, url):
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


def fetch_trifecta_result(sess, date_str, stadium, race_no):
    """
    結果ページから3連単の的中組番と払戻金(100円あたり)を取得。
    戻り値: (combo:str, payout:int) 例 ("1-3-2", 1850)。取得失敗時 (None, None)。
    """
    url = f"{BOATRACE_BASE}/raceresult?jcd={stadium}&hd={date_str}&rno={race_no}"
    resp = sess.get(url)
    if resp is None or resp.status_code != 200:
        return None, None
    # HTMLからタグを除去してテキスト化し、「3連単 → 組番 → 金額」の並びを正規表現で拾う
    text = re.sub(r"<[^>]+>", " ", resp.text)
    text = re.sub(r"\s+", " ", text)
    m = re.search(r"3連単\s*(\d)\s*-\s*(\d)\s*-\s*(\d)\s*[¥\\]?\s*([\d,]+)", text)
    if not m:
        # レース不成立・返還等のケース
        if "不成立" in text or "返還" in text:
            return "不成立", 0
        print(f"  [警告] {STADIUM_NAMES.get(stadium, stadium)}{race_no}R: "
              f"結果を解析できませんでした(未確定またはHTML構造変化)", file=sys.stderr)
        return None, None
    combo = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    payout = int(m.group(4).replace(",", ""))
    return combo, payout


def load_picks(report_path):
    """スクリーナーのCSVレポートを読み込む。"""
    picks = []
    with open(report_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            stadium_code = NAME_TO_CODE.get(row["場"], row["場"])
            picks.append({
                "stadium": stadium_code,
                "stadium_name": row["場"],
                "race_no": int(row["レース"].replace("R", "")),
                "combo": row["買い目"],
                "prob": float(row["推定確率%"]),
                "odds_scan": float(row["オッズ(取得時)"]),
                "ev_scan": float(row["回収率%"]),
            })
    return picks


def append_history(history_path, rows):
    exists = Path(history_path).exists()
    fields = ["date", "stadium", "race", "combo", "prob%", "odds_at_scan",
              "result_combo", "hit", "payout", "pnl"]
    with open(history_path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def build_dashboard(history_path, out_path):
    rows = []
    with open(history_path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return

    n = len(rows)
    hits = sum(1 for r in rows if r["hit"] == "○")
    invested = n * BET_UNIT
    returned = sum(int(r["payout"]) for r in rows if r["hit"] == "○")
    roi = returned / invested * 100 if invested else 0
    pnl = returned - invested

    # 日別集計
    daily = {}
    for r in rows:
        d = daily.setdefault(r["date"], {"n": 0, "hit": 0, "ret": 0})
        d["n"] += 1
        d["hit"] += 1 if r["hit"] == "○" else 0
        d["ret"] += int(r["payout"]) if r["hit"] == "○" else 0

    daily_rows = "\n".join(
        f"<tr><td>{d}</td><td>{v['n']}</td><td>{v['hit']}</td>"
        f"<td>{v['ret'] - v['n']*BET_UNIT:+,}円</td></tr>"
        for d, v in sorted(daily.items(), reverse=True)
    )
    recent = "\n".join(
        f"<tr><td>{r['date'][4:6]}/{r['date'][6:8]}</td><td>{STADIUM_NAMES.get(r['stadium'], r['stadium'])}{r['race']}R</td>"
        f"<td class='combo'>{r['combo']}</td>"
        f"<td class='{'hit' if r['hit']=='○' else 'miss'}'>{r['hit']}</td>"
        f"<td>{r['result_combo']}</td>"
        f"<td>{int(r['pnl']):+,}円</td></tr>"
        for r in rows[-50:][::-1]
    )
    pnl_color = "#4ade80" if pnl >= 0 else "#f87171"

    html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>競艇EV 収支ダッシュボード</title>
<style>
body{{font-family:'Hiragino Kaku Gothic ProN',sans-serif;background:#0d1b31;color:#e8edf5;
padding:16px;max-width:640px;margin:0 auto}}
h1{{font-size:18px}} h2{{font-size:13px;color:#6fd3dd;letter-spacing:.12em;margin:22px 0 8px}}
.stats{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:14px}}
.stat{{background:rgba(20,34,58,.85);border:1px solid rgba(120,160,210,.16);
border-radius:12px;padding:12px}}
.stat .l{{font-size:10px;color:#8fa3bf}} .stat .v{{font-size:22px;font-weight:800;margin-top:2px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:7px 5px;border-bottom:1px solid rgba(120,160,210,.12);text-align:center}}
th{{color:#6fd3dd;font-size:10px;letter-spacing:.08em}}
.combo{{font-weight:800}} .hit{{color:#4ade80;font-weight:800}} .miss{{color:#5b6b84}}
.warn{{background:rgba(60,40,20,.5);border:1px solid rgba(245,197,24,.3);border-radius:10px;
padding:10px 12px;font-size:11px;color:#d9c48a;line-height:1.7;margin-top:18px}}
</style></head><body>
<h1>競艇EV 収支ダッシュボード</h1>
<p style="font-size:11px;color:#8fa3bf">1点{BET_UNIT}円購入想定のシミュレーション / 更新: {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
<div class="stats">
<div class="stat"><div class="l">通算損益</div><div class="v" style="color:{pnl_color}">{pnl:+,}円</div></div>
<div class="stat"><div class="l">回収率</div><div class="v" style="color:{pnl_color}">{roi:.1f}%</div></div>
<div class="stat"><div class="l">的中 / 購入点数</div><div class="v">{hits} / {n}</div></div>
<div class="stat"><div class="l">的中率</div><div class="v">{hits/n*100:.1f}%</div></div>
</div>
<h2>日別収支</h2>
<table><tr><th>日付</th><th>点数</th><th>的中</th><th>損益</th></tr>{daily_rows}</table>
<h2>直近の照合結果(最大50件)</h2>
<table><tr><th>日付</th><th>レース</th><th>買い目</th><th>結果</th><th>的中組番</th><th>損益</th></tr>
{recent}</table>
<div class="warn">⚠ 回収率が100%を下回り続ける場合、それがこのモデルの実力です。
閾値やモデルを調整する前に、まず十分なサンプル数(最低100点以上)を貯めて判断してください。
数日の結果で「勝てる/勝てない」を判断するのはどちらの方向でも誤りです。</div>
</body></html>"""
    Path(out_path).write_text(html, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="競艇 結果照合・収支トラッカー")
    parser.add_argument("--date", default=datetime.now().strftime("%Y%m%d"),
                        help="対象日 YYYYMMDD(既定: 今日)")
    parser.add_argument("--report-dir", default=".", help="ev_report_*.csv のあるディレクトリ")
    parser.add_argument("--out", default=".", help="history.csv / dashboard.html の出力先")
    args = parser.parse_args()

    date_str = args.date
    report_path = Path(args.report_dir) / f"ev_report_{date_str}.csv"
    if not report_path.exists():
        print(f"レポートが見つかりません: {report_path}")
        print("先に kyotei_daily_screener.py を実行してください。")
        return

    picks = load_picks(report_path)
    if not picks:
        print("照合対象の買い目がありません。")
        return

    # 同じレースは1回だけ取得
    races = sorted({(p["stadium"], p["race_no"]) for p in picks})
    print(f"照合対象: {len(picks)}点 / {len(races)}レース")
    print(f"推定所要時間: 約{len(races) * REQUEST_INTERVAL / 60:.1f}分\n")

    sess = RateLimitedSession()
    results = {}
    for stadium, race_no in races:
        combo, payout = fetch_trifecta_result(sess, date_str, stadium, race_no)
        results[(stadium, race_no)] = (combo, payout)
        name = STADIUM_NAMES.get(stadium, stadium)
        print(f"{name} {race_no:2d}R → {combo or '取得失敗'}"
              + (f" ¥{payout:,}" if payout else ""))

    history_rows = []
    for p in picks:
        combo, payout = results.get((p["stadium"], p["race_no"]), (None, None))
        if combo is None:
            continue  # 未確定レースは次回実行時に照合(historyに書かない)
        hit = combo == p["combo"]
        history_rows.append({
            "date": date_str,
            "stadium": p["stadium"],
            "race": p["race_no"],
            "combo": p["combo"],
            "prob%": p["prob"],
            "odds_at_scan": p["odds_scan"],
            "result_combo": combo,
            "hit": "○" if hit else "×",
            "payout": payout if hit else 0,
            "pnl": (payout - BET_UNIT) if hit else -BET_UNIT,
        })

    if not history_rows:
        print("\n確定済みの結果がありませんでした(レース未終了の可能性)。")
        return

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    history_path = out_dir / "history.csv"
    append_history(history_path, history_rows)
    build_dashboard(history_path, out_dir / "dashboard.html")

    hits = sum(1 for r in history_rows if r["hit"] == "○")
    pnl = sum(r["pnl"] for r in history_rows)
    print(f"\n本日: {hits}/{len(history_rows)}的中 損益{pnl:+,}円(1点{BET_UNIT}円想定)")
    print(f"履歴: {history_path}")
    print(f"ダッシュボード: {out_dir / 'dashboard.html'}")


if __name__ == "__main__":
    main()
