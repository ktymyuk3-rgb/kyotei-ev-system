# 競艇 期待値スクリーニングシステム

毎朝、全開催場のレースをスキャンして期待値の高い3連単の買い目を抽出し、
毎晩、結果と照合して的中率・回収率を記録する自動システムです。

## 構成

```
kyotei_daily_screener.py   朝: 全レーススキャン → docs/ev_report_日付.html
kyotei_result_checker.py   夜: 結果照合 → docs/dashboard.html
requirements.txt           依存ライブラリ
.github/workflows/
  morning.yml              毎朝9時(JST)に自動実行
  evening.yml              毎晩22時(JST)に自動実行
docs/                      GitHub Pagesで公開されるフォルダ
  index.html               トップページ
```

## セットアップ手順

### STEP 1: まずローカルで動作確認(推奨)

```bash
pip install -r requirements.txt
python kyotei_daily_screener.py --stadiums 12 --out docs   # まず1場だけで試す(約10分)
# → docs/ev_report_日付.html が生成されればOK

# レース終了後の夜に:
python kyotei_result_checker.py --report-dir docs --out docs
# → docs/dashboard.html が生成されればOK
```

### STEP 2: GitHubへアップロード

1. GitHubで新規リポジトリを作成(Public ※Pages無料公開に必要)
2. このフォルダの中身をすべてアップロード
   - Webからアップロードする場合、`.github/workflows/` の2ファイルも
     忘れずに(「Add file → Create new file」でパスごと入力可能)

### STEP 3: GitHub Pagesを有効化

1. リポジトリの Settings → Pages
2. Source: 「Deploy from a branch」/ Branch: `main` / フォルダ: `/docs`
3. 数分後に `https://ユーザー名.github.io/リポジトリ名/` で公開される
   → スマホでブックマーク

### STEP 4: Actionsの動作確認

1. リポジトリの Actions タブ → 「morning-screener」→「Run workflow」で手動実行
2. 完了後、docs/ にレポートが追加されコミットされていれば成功
3. 以後は毎朝9時・毎晩22時に自動実行

## 注意事項

- **アクセス間隔4秒は変更しないでください。** サーバー負荷を避けるための設計です。
- オッズは取得時点のもの。実際の購入前に必ず最新オッズで確認を。
- 期待値はモデル推定であり、的中や利益を保証するものではありません。
  控除率25%の構造上、平均期待値は必ずマイナスです。
- まずは1〜2ヶ月、購入せずにダッシュボードで実力を検証(ペーパートレード)
  することを強く推奨します。判断には最低100点以上のサンプルが必要です。
- GitHub Actionsの共有IPからのアクセスがブロックされる場合があります。
  その場合はローカルPCのcron/タスクスケジューラでの運用に切り替えてください
  (スクリプトはそのまま使えます)。

## よくある調整

- 抽出が多すぎる/少なすぎる → morning.yml の実行コマンドに `--threshold 1.3` などを追加
- 特定の場だけ対象にする → `--stadiums 12,22,24` を追加(実行時間も短縮)
