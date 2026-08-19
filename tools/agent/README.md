# ローカルエージェント構成

Intel Arc A770機で動く、複数の役(role)が交代で動くローカルLLMエージェント。
GPU(VRAM)が1本しかないため、複数の役を同時には動かせない。そのため
「役の切り替え＝別プロセスへの引き継ぎ」ではなく、**同一プロセス内の入れ子の関数呼び出し**
として実装している（詳細は後述）。

[NOTE] 実装の経緯・検証結果は [`planning/chat-history-summary.md`](../../planning/chat-history-summary.md)
にも記録がある。本ドキュメントは現在の仕様のリファレンス。

---

## 役(role)一覧

「役」はモデルと初期プロンプト（＋使えるtool）の組み合わせに過ぎない。
その定義は `roles/<role_id>/` ディレクトリに切り出してあり、コードではなく
データ（`role.json` + `prompt.txt`）として持つ。

| 役 | 定義 | 実行する側のモジュール | できること |
|----|------|----------------------|-----------|
| 雑談役 (chat) | `roles/chat/` | `chat_agent.py` | 会話のみ。ファイル操作・コマンド実行は不可 |
| Execute役 (execute) | `roles/execute/` | `arc_agent.py` | コマンド実行・ファイル編集・共有メモ書き込み |

### 新しい役を追加するには

`roles/<role_id>/` に以下の2ファイルを置く。

```
roles/<role_id>/role.json    { "display_name": "...", "model": "...", "tools": ["read_file", "remember", ...] }
roles/<role_id>/prompt.txt   システムプロンプト本文
```

`tools` に書けるのは `scripts/tools.py` の `TOOL_REGISTRY` に登録済みのtool名だけ
（`scripts/role_loader.py` の `load_role()` が読み込み時に検証する）。

[NOTE] これで役の「定義」（モデル・プロンプト・使えるtoolの一覧）はファイルだけで
完結するが、その役を「実際に呼び出す」コード（今なら`chat_agent.py`が
`arc_agent.start_interactive_chat()`を呼ぶ部分）は別途必要。
tool呼び出しの実処理（`run_command`等）も`arc_agent.py`側の実装に依存しているため、
全く新しい種類の役（例: 検索専用でファイル編集はしない役）を追加する場合は、
`role.json`の`tools`を絞るだけで対応できるが、Execute系の実処理を使わない
役を作る場合はコード側の対応が別途必要になる。

起動はユーザーが `chat_agent.py` を実行するところから始まる:

```powershell
cd tools\agent
python chat_agent.py
```

`arc_agent.py` を直接起動することもできる（従来通りメニュー選択式で動く。手動でExecute役
だけを使いたい時用）。ただし通常はユーザーが直接起動するのは `chat_agent.py` のみでよい。

---

## 全体の流れ（入れ子構造）

```
main() in chat_agent.py
  └─ run_chat_loop()                       ← 雑談役。ユーザーと直接対話する
       │  ユーザーの要望が実作業を要すると判断
       │  handoff_to_execute が呼ばれる
       ▼
     run_execute_and_wait()
       │  自分(雑談役)のモデルをVRAMから解放
       ▼
     arc_agent.start_interactive_chat(is_nested=True)   ← Execute役。ここも同じプロセス
       │  コマンド実行・ファイル編集を行う
       │  作業完了と判断
       │  return_to_chat が呼ばれる → 要約文字列を return
       ▼
     run_execute_and_wait() に戻ってくる
       │  Execute役のモデルは自分の中でVRAM解放済み
       │  雑談役のモデルを再ロード
       ▼
     run_chat_loop() の続きに戻る            ← 要約を会話に注入して雑談続行
```

`arc_agent.start_interactive_chat()` は普通のPython関数で、呼ばれたら実行し、
終わったら（`return_to_chat` が呼ばれるか、単に終了すれば）呼び出し元に戻ってくる。
別プロセスを起動することも、何かを `kill` することも一切ない。

### なぜプロセスを分けず、この形にしたか

最初はプロセスを分け、役の切り替えのたびに「前の役をkillして次の役を新規プロセスで起動する」
設計で作っていた（`active_role.json` でPIDを記録し、起動時に前の役を`taskkill`する等）。
しかし「役はモデルと初期プロンプトの違いでしかない」と気づき、入れ子の関数呼び出しに
置き換えた。GPU制約や起動失敗への配慮は、この形でもそのまま満たされる。

- **VRAM制約（同時に1モデルまで）**: 呼び出す前に自分のモデルをアンロードし、
  呼び出し先が終わるまで自分は関数呼び出しの中でブロックされたまま待つ。
  同時に2つのモデルがロードされることはない。
- **起動失敗への配慮**: 別プロセスを新規に起動するわけではないので、
  「起動したはずが失敗する」という事故そのものが起こり得ない。
- **副次効果**: コンソールウィンドウが新しく開いたり閉じたりすることが無くなった。
  Execute役が例外で落ちても、呼び出し元(雑談役)の`try/except`で捕まえて
  会話を継続できる（プロセスごと落ちる旧設計より頑健）。
- 将来Plan役・Review役等を追加する場合も、必要な役の関数を呼んで結果を受け取るだけでよく、
  入れ子は自然に深くなる（プロセス管理の仕組みを都度増やす必要が無い）。

---

## 状態ファイル（`tools/agent/` 直下、実行時生成・gitignore対象）

| ファイル | 役割 | 寿命 |
|---------|------|------|
| `shared_memory.md` | 全役共通の「忘れてはいけない事項」。追記型 | 永続（消えない。ユーザーが直接編集してもよい） |
| `logs/{role}_{timestamp}.log` | セッションごとの会話ログ。ターンごとに逐次追記 | 永続（古いものは手動で整理する想定） |

役の切り替え自体はプロセス内の関数呼び出しなので、以前あった `active_role.json`
（PID記録）や `relay.json`（引き継ぎメッセージ）は不要になり廃止した。

---

## tool一覧

Ollamaのtool calling（function calling）で実装している。モデルによっては構造化された
`message.tool_calls` を返さず、JSON文字列を`content`にそのまま出力することがあるため、
`scripts/tools.py` の `tool_call_from_content()` がその救済フォールバックとして働く。
1つの返答に複数のtool呼び出しJSON（```json```フェンスが複数）が並ぶこともあるため、
見つかった有効なものを全て抽出する（最初の1個だけを取り出す実装だと、2個以上並んだ時に
まとめて壊れたJSONとして読めてしまい、両方とも無視される不具合があった）。

### 雑談役 (`CHAT_TOOLS`)

| tool | 引数 | 用途 |
|------|------|------|
| `handoff_to_execute` | `instructions`, `reason` | Execute役を呼び出す |
| `remember` | `note` | 共有メモに書き残す |

### Execute役 (`TOOLS`)

| tool | 引数 | 用途 |
|------|------|------|
| `execute_command` | `command` | PowerShellコマンドを1つ実行 |
| `edit_file` | `file`, `search`, `replace` | SEARCH/REPLACE形式のピンポイント編集 |
| `read_file` | `file` | ファイルを行番号付きで読む（文字化け対策） |
| `remember` | `note` | 共有メモに書き残す |
| `return_to_chat` | `summary` | 作業完了。雑談役に会話を戻す（関数のreturn） |

いずれも1返答につき1回だけ呼ぶ設計（各役の`roles/<role_id>/prompt.txt`で明示）。

---

## モデル選定の経緯

[NOTE] 検証時点(Ollama 0.32.6)での結果。バージョンが上がれば変わる可能性がある。

雑談役のモデルは複数回入れ替えて検証した:

- **deepseek-r1:14b**: `handoff_to_execute` を実際には呼ばず、言葉で「引き継ぎます」と
  説明するだけで終わることが3回中3回発生。判断はできているが、tool呼び出しという
  手続きに変換できていなかった。
- **llama3.1:8b**: tool呼び出し自体は構造化`tool_calls`で確実に返る。しかし
  「やぁ」のような雑談にまでtoolを呼んでしまう誤検知が、temperatureを0まで
  下げても直らなかった（tools有りだと何か呼ばなければと思い込む癖）。
  toolを1個(`handoff_to_execute`のみ)に絞っても改善せず、むしろ全メッセージで
  誤発火するようになり悪化した。
- **qwen2.5-coder:14b**: 構造化`tool_calls`は返さないが、`tool_call_from_content()`の
  フォールバックで拾える形のJSONを出す。プロンプトを強化（「toolは例外処理」
  「rememberは価値ある事実がある時だけ」を明記）した上で検証したところ、
  雑談には反応せず・曖昧な依頼は聞き返し・明確な依頼は正しく引き継ぐ、と
  最もバランスが良かった。現在の既定モデル。

Execute役の既定モデルも同じ qwen2.5-coder:14b（`MODELS["6"]`）。
`roles/execute/prompt.txt` の手本セクションが「→ tool(args) を呼ぶ。」という擬似コード表記だった時は、
モデルがそれをそのまま文章として書き写すだけでtoolを呼ばない不具合があった。
手本は「実際にtool呼び出し機能を使う」ことを明記し、コピー可能な疑似コードを避ける形に直した。

[WARNING] 小型ローカルモデルは判断を誤ることがある（例: 相対パスでのファイル読み込みに
1回失敗しただけで「ファイルが存在しない」と誤った結論をremember/return_to_chatに
書いてしまうケースを確認済み）。共有メモや報告内容は鵜呑みにせず、重要な判断は
ユーザー自身で確認すること。

---

## 既知の不具合と対策

### VRAM/メモリのリーク（修正済み）

`cleanup_processes()` が対象にしていたプロセス名が古く、実際にモデルを保持している
ランナープロセスの名前（`llama-server.exe`。検証時点のOllama 0.32.6）と一致していなかった。
そのため `ollama.exe` 本体を殺しても、ランナーだけ孤児化してVRAM/メモリを掴んだまま
残り続けていた（セッションを重ねるたびに蓄積し、数十GB分溜まった実績あり）。
`scripts/ollama.py` の `cleanup_processes()` / `startup_cleanup()` を修正済み。

### セッションが想定外に終了した場合の後片付け

`run_chat_loop()` / `start_interactive_chat()` は、想定していない例外（バグ等）で
関数を抜けた場合でも `finally` でVRAM解放だけは必ず行うようにしてある。

---

## ファイル構成

```
tools/agent/
├── chat_agent.py            雑談役 本体・プログラムの入口（ユーザーが起動するのはこれ）
├── arc_agent.py              Execute役 本体（chat_agent.pyから直接importして呼ばれる）
├── README.md                    このファイル
├── roles/
│   ├── chat/
│   │   ├── role.json             雑談役の定義（モデル・使うtool名）
│   │   └── prompt.txt            雑談役システムプロンプト
│   └── execute/
│       ├── role.json             Execute役の定義（モデル・使うtool名）
│       └── prompt.txt            Execute役システムプロンプト
└── scripts/
    ├── config.py               定数（ファイル名等）
    ├── ollama.py               Ollamaサーバーのライフサイクル管理（起動・停止・VRAM解放）
    ├── tools.py                  tool定義・TOOL_REGISTRY・tool_calls⇔内部アクション形式の変換
    ├── role_loader.py            roles/ からの役の読み込み
    └── memory.py                 セッションログ・共有メモ(shared_memory.md)の読み書き
```
