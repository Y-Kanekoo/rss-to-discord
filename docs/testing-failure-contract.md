# 通知失敗・状態保全の機能別テスト方針

## 目的と判定

失敗を成功に見せず、確認済み送信履歴を保全し、未確認の配送を成功扱いしないことを検証します。
元のmain `2f33780a7b8000b226ea01b55c75caaa382b9f94` では既存16件が成功する一方、
先に追加した9件の回帰テストは失敗を再現しました（subtestを含む14 failure / 1 error）。
旧テストで意図的に固定していた破損JSONの空状態fallbackは、fail-closedの期待値へ変更します。

| 機能・リスク | unit | integration / CLI | 合格条件 |
|---|---|---|---|
| 状態読み込み | 欠落、JSON破損、型不正、UTF-8、重複キー、権限エラー | 破損状態で実CLI終了 | 初回以外はfail-closed、元bytes不変、送信0 |
| 原子的保存 | dump/replace失敗と一時ファイル掃除 | 保存不可、2件目成功後のcheckpoint失敗 | 旧stateを保持、追加送信停止、未保存成功件数を報告 |
| 部分失敗 | 成功だけ記録 | HTTP 204/500、実CLI exit、再実行 | exit 1、成功済みを再送せず失敗分だけ再試行 |
| 初回制限 | 最新5件、初回失敗後の追加記事 | 2回連続実行 | 前回失敗分が最新5件制限で消えない |
| レート制限 | 429→成功、429→429、不正待機値 | CLI 429再失敗 | 明示429だけ1回再試行、0〜60秒の有限待機 |
| 配送結果不明 | Timeout/ConnectionError | 429→成功と別記事timeout | 即時再送せず、unknownを報告、成功扱いしない |
| GUID重複・不正記事 | 重複GUID、不正GUID/title | CLIに重複記事 | GUIDごとに1回、他の正常記事は処理可能 |
| 設定とdry-run | 未設定/空白Webhook、明示dry-run | 実__main__経由の両モード | 未設定はexit 1、dry-runは送信/保存0でexit 0 |
| エラー機密性 | feed/HTTP/state/git例外に偽token | CLIのHTTPError/Timeout出力 | 偽token・URL・例外本文をstdout/stderrへ出さない |
| 永続化の競合 | GUID和集合、時刻、未知metadata衝突 | 最新main、fetch後の並行code+state commit、初回欠落と並行削除 | forceなし、両方のGUIDと最新コードを保持 |
| push障害 | ログの秘匿 | 3回拒否、成功後の応答不明、remote破損 | 成功済みを保持、重複commit回避、検証済み回復snapshot |
| Actions契約 | YAML条件の回帰検査 | 同じsuiteをpush/PR CIで実行 | 部分失敗後もpersist、job失敗維持、既存cron/権限維持 |

## 実行

```sh
uv sync --locked --no-dev
uv run --locked --no-dev python -m unittest discover -s tests -v
uv pip check
uv lock --check --offline
git diff --check
```

HTTP境界はモックにし、socket接続とDNSをブロックします。CLIも同じ遮断を子プロセス内で適用します。
Git integrationは一時ディレクトリ内のbare remoteのみを使い、`GIT_ALLOW_PROTOCOL=file`で
ネットワークprotocolを禁止し、system/global Git設定を無効にして認証情報を不要にします。
stateはすべて一時領域の合成データです。テストは本番stateを読み書きしません。

## マージ条件

- 同じ最終headに対する全テスト・lock整合・負の検査・独立レビューが成功
- 最新mainの送信済みstate blobをPR差分に含めず、synthetic mergeでも保全
- push/PRはテストだけを起動すること、mainへの通常mergeも通知を即時起動しないことを確認
- 未解決review・失敗check・競合があればマージしない。force push、admin bypass、branch削除を使わない
- マージ後のmainテストCIがterminalになるまで確認

## 未検証と残余リスク

本番Discordへの送信、実ネットワーク障害、本番GitHub tokenの権限、artifact実アップロード、
runnerの物理的クラッシュはオフラインsuiteでは検証できません。特に通知workflowの実行は
既存の予定実行に委ね、テスト目的で手動起動しません。監視の緑は本番配信成功の保証ではありません。
