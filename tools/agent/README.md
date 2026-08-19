# ローカルエージェント構成

Intel Arc A770機で動く、複数の役(role)が交代で動くローカルLLMエージェント。
GPU(VRAM)が1本しかないため、複数の役を同時には動かせない。そのため
「役の切り替え＝別プロセスへの引き継ぎ」ではなく、**同一プロセス内の入れ子の関数呼び出し**
として実装している（詳細は後述）。

[NOTE] 実装の経緯・検証結果は [`planning/chat-history-summary.md`](../../planning/chat-history-summary.md)
にも記録がある。本ドキュメントは現在の仕様のリファレンス。

[設計目標] 役が増えても破綻しないこと。具体的には: (1) どの役からどの役へも
共通の形（`handoff_to_role` / `return_to_caller`）で引き継げる、(2) 引き継ぎ先は
実際の直近の会話を受け取れる、(3) 役をまたいでも1つの共通ログで会話の続きが追える、
(4) 各役は自分の専門性を認識し、専門外は無理に自分でやろうとせず適切な役に
任せる、の4点。詳細は「新しい役を追加するには」を参照。

---

## 役(role)一覧

「役」はモデルと初期プロンプト（＋使えるtool）の組み合わせに過ぎない。
その定義は `roles/<role_id>/` ディレクトリに切り出してあり、コードではなく
データ（`role.json` + `prompt.txt`）として持つ。

| 役 | 定義 | 実行する側のモジュール | できること |
|----|------|----------------------|-----------|
| 雑談役 (chat) | `roles/chat/` | `chat_agent.py` | 会話のみ。ファイル操作・コマンド実行は不可 |
| Execute役 (execute) | `roles/execute/` | `arc_agent.py` | コマンド実行・ファイル編集・共有メモ書き込み |
| Review役 (review) | `roles/review/` | `arc_agent.py`（Execute役と共用） | ファイルを読んで指摘するだけ。編集不可（`tools`に`edit_file`を含めていない） |

[NOTE] Review役はExecute役と同じ`arc_agent.py`を`module`として使うが、`tools`は
`read_file`/`execute_command`/`remember`/`return_to_caller`だけに絞ってあり、
`edit_file`は持たない。実装を増やさず、role.jsonの`tools`を絞るだけで
「調査はするが変更はしない役」を作れることの実例。

### 新しい役を追加するには

`roles/<role_id>/` に以下の2ファイルを置く。

```
roles/<role_id>/role.json
{
  "display_name": "...",
  "specialty": "この役の専門性（1行）。handoff_to_roleの説明文と、自分自身への
                自己認識プロンプトの両方に自動で使われる。",
  "model": "...",
  "tools": ["read_file", "remember", ...],
  "can_handoff_to": ["execute", ...],   // 他の役に引き継げるなら列挙。無ければ省略可
  "module": "some_role_module"          // 引き継ぎ先として呼ばれる役だけ必要（後述）
}

roles/<role_id>/prompt.txt   システムプロンプト本文
```

`tools` に書けるのは `scripts/tools.py` の `TOOL_REGISTRY` に登録済みのtool名だけ
（`scripts/role_loader.py` の `load_role()` が読み込み時に検証する）。
`handoff_to_role` はここには書かない。`can_handoff_to` を書くと自動的に付与される
（後述）。

[NOTE] 役の「定義」（モデル・プロンプト・使えるtool・引き継ぎ先・専門性）はファイル
だけで完結する。ただし tool呼び出しの実処理（`run_command`等）は実装コード側に依存
するため、既存のExecute役と全く違う種類の役（例: Web検索専用でファイル編集はしない
役）を追加する場合は、その役自身の実装（Pythonモジュール）を書く必要がある。

#### 引き継ぎ（handoff）は role_id 名指しではなく共通の形

役Aの `role.json` に `"can_handoff_to": ["execute", "review"]` と書くと、
`scripts/role_loader.py` が自動的に `handoff_to_role` という1つのtool
（`role_id` はenumで `execute`/`review` に制限される）をAのtool一覧に追加し、
Aのプロンプトの末尾にも「自分の専門性はこれ、それ以外はこの役に引き継げ」という
自己認識ブロックを自動で注入する。これにより：

- 呼び出す側（chat_agent.py等）は `handoff_to_role` という1つのtool呼び出しだけを
  見ればよく、`handoff_to_execute` のような役名固定のtoolを役の数だけ増やす必要が無い。
- 引き継ぎ先の実際の起動は `scripts/dispatch.py` の `invoke_role(base_dir, role_id, ...)`
  が担う。`role.json` の `"module"` を見て `importlib` でそのモジュールを動的に
  importし、`"entry"`（省略時は `start_interactive_chat`）という名前の関数を
  共通のシグネチャで呼ぶ。呼び出し元のコード（`chat_agent.py`）は、どの役を
  呼ぶ時も同じ`invoke_role()`しか使わないため、新しい役を追加してもこのファイルは
  変更不要（`roles/<role_id>/role.json`の`can_handoff_to`に追記するだけでよい）。
- 引き継ぎ先として呼ばれる役の実装は、次の契約を満たす関数を公開すること:
  ```python
  def start_interactive_chat(model_name, exec_mode, server_proc, *,
                              initial_message=None, is_nested=False,
                              log_path=None, role_id=None, **kwargs): ...
  # 戻り値: return_to_callerが呼ばれれば要約文字列、それ以外の終了ならNone
  ```
  [IMPORTANT] `role_id`は「複数の役が同じモジュールを共用できる」ようにするための
  引数。実装側はモジュール読み込み時に固定されたグローバルなroleを常に使うのではなく、
  `role_id`が渡されたらそちらの`roles/<role_id>/`定義を都度読み込むこと（`arc_agent.py`
  の実装を参照）。これを怠ると、tool一覧を絞ったつもりの役（例: Review役に`edit_file`を
  持たせない）が、実際にはモジュール既定の役と同じ全toolを使えてしまうという事故が起こる
  （Review役を追加した際に実際に踏んだ不具合。詳細は「既知の不具合と対策」参照）。
- 戻る側も `return_to_caller`（引数`summary`のみ）という1つのtoolに統一されている。
  「雑談役に戻る」という名前ではなく「呼び出し元に戻る」なので、将来Execute役以外の
  役からExecute役を呼ぶケースが増えても意味が破綻しない。

[NOTE] ストリーム応答の表示（点字スピナー・「思考中」→「出力中」のラベル切り替え・
トークン数表示）は`scripts/display.py`の`stream_chat_response()`に共通化してあり、
`chat_agent.py`/`arc_agent.py`どちらも同じものを使う。以前は`chat_agent.py`だけ
別の簡易実装（スピナー無し）を個別に持っていて表示が役ごとに揃っていなかったが、
これは役の「定義」の話ではなく実装の重複だったため、ここに一本化した。

起動はユーザーが `chat_agent.py` を実行するところから始まる:

```powershell
cd tools\agent
python chat_agent.py
```

`arc_agent.py` を直接起動することもできる（従来通りメニュー選択式で動く。手動でExecute役
だけを使いたい時用）。ただし通常はユーザーが直接起動するのは `chat_agent.py` のみでよい。

`arc_agent.py`単体起動時のメニューには `[t] テストモード` がある。`scripts.config.MODELS`
の決め打ちリスト（実機に入っていないモデルも並ぶ）ではなく、Ollamaに実際にpull済みの
モデルを`ollama list`相当のAPIで取得して一覧表示し、番号かモデル名を直接入力して起動できる。
`num_ctx`（コンテキスト長）もその場で自由に指定できる（空Enterで既定の8192）。
新しいモデルを試す・大きいコンテキストで挙動を見る、といった検証用。

---

## 全体の流れ（入れ子構造）

```
main() in chat_agent.py
  └─ run_chat_loop()                       ← 雑談役。ユーザーと直接対話する
       │  ユーザーの要望が実作業を要すると判断
       │  handoff_to_role(role_id="execute") が呼ばれる
       │  直近の会話の抜粋（要約ではなく実データ）を instructions に添えて渡す
       ▼
     run_role_and_wait() → scripts.dispatch.invoke_role()
       │  自分(雑談役)のモデルをVRAMから解放
       │  role.jsonの"module"を見てimportlibで動的import
       ▼
     arc_agent.start_interactive_chat(is_nested=True, log_path=共通ログ)  ← Execute役。同じプロセス
       │  コマンド実行・ファイル編集を行う（雑談役と同じログファイルに追記）
       │  作業完了と判断
       │  return_to_caller が呼ばれる → 要約文字列を return
       ▼
     invoke_role() に戻ってくる
       │  Execute役のモデルは自分の中でVRAM解放済み
       │  雑談役のモデルを再ロード
       ▼
     run_chat_loop() の続きに戻る            ← 要約を会話に注入して雑談続行
```

引き継ぎ先の`start_interactive_chat()`は普通のPython関数で、呼ばれたら実行し、
終わったら（`return_to_caller` が呼ばれるか、単に終了すれば）呼び出し元に戻ってくる。
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
| `logs/{role}_{timestamp}.log` | セッション全体の会話ログ。ターンごとに逐次追記 | 永続（古いものは手動で整理する想定） |

役の切り替え自体はプロセス内の関数呼び出しなので、以前あった `active_role.json`
（PID記録）や `relay.json`（引き継ぎメッセージ）は不要になり廃止した。

[NOTE] ログファイルは役ごとではなく、**トップレベルのセッション単位で1つ**。
`chat_agent.main()` が最初に1回 `start_session_log()` で作ったログファイルの
パス(`log_path`)を、引き継ぎのたびに `invoke_role()` → 引き継ぎ先の
`start_interactive_chat(..., log_path=...)` へそのまま渡し、引き継ぎ先も同じ
ファイルに追記する。以前は役ごとに別ファイル（`chat_*.log`と`execute_*.log`が
別々）だったため、雑談役に戻った時に何が起きたかを追うには2つのファイルを
タイムスタンプで突き合わせる必要があったが、今は1ファイルで完結する。
`scripts/memory.py`の`append_role_transition()`が引き継ぎ/復帰のたびに
`=== 引き継ぎ: chat → execute ===` / `=== 復帰: execute → chat ===` という
区切りをログに書き込むため、ファイルを開くだけで会話の続きが追える。

---

## tool一覧

Ollamaのtool calling（function calling）で実装している。モデルによっては構造化された
`message.tool_calls` を返さず、JSON文字列を`content`にそのまま出力することがあるため、
`scripts/tools.py` の `tool_call_from_content()` がその救済フォールバックとして働く。
1つの返答に複数のtool呼び出しJSON（```json```フェンスが複数）が並ぶこともあるため、
見つかった有効なものを全て抽出する（最初の1個だけを取り出す実装だと、2個以上並んだ時に
まとめて壊れたJSONとして読めてしまい、両方とも無視される不具合があった）。

### 役をまたいで共通のtool（`can_handoff_to`があれば自動付与）

| tool | 引数 | 用途 |
|------|------|------|
| `handoff_to_role` | `role_id`, `instructions`, `reason` | 指定した役を呼び出す（`role_id`は自分の`can_handoff_to`の範囲にenumで制限される） |
| `return_to_caller` | `summary` | 作業完了。自分を呼び出した側に会話を戻す（関数のreturn） |
| `remember` | `note` | 共有メモに書き残す |

### 雑談役固有

| tool | 引数 | 用途 |
|------|------|------|
| `handoff_to_role` | `role_id`（`"execute"`か`"review"`）, `instructions`, `reason` | `roles/chat/role.json`の`can_handoff_to`が`["execute", "review"]`なのでこの2つに限定 |
| `remember` | `note` | 共有メモに書き残す |

### Execute役固有

| tool | 引数 | 用途 |
|------|------|------|
| `execute_command` | `command` | PowerShellコマンドを1つ実行 |
| `edit_file` | `file`, `search`, `replace` | SEARCH/REPLACE形式のピンポイント編集 |
| `read_file` | `file` | ファイルを行番号付きで読む（文字化け対策） |
| `remember` | `note` | 共有メモに書き残す |
| `return_to_caller` | `summary` | 作業完了。呼び出し元に会話を戻す（関数のreturn） |

### Review役固有

| tool | 引数 | 用途 |
|------|------|------|
| `execute_command` | `command` | 調査目的のみ（`is_read_only_command()`の判定は変わらないため、変更系コマンドは承認要求される） |
| `read_file` | `file` | ファイルを行番号付きで読む |
| `remember` | `note` | 共有メモに書き残す |
| `return_to_caller` | `summary` | 確認完了。呼び出し元に会話を戻す（関数のreturn） |
| ~~`edit_file`~~ | - | `role.json`の`tools`に含めていないため使えない |

いずれも1返答につき1回だけ呼ぶ設計（各役の`roles/<role_id>/prompt.txt`で明示）。

---

## モデル選定の経緯

[NOTE] 検証時点(Ollama 0.32.6)での結果。バージョンが上がれば変わる可能性がある。

雑談役のモデルは複数回入れ替えて検証した:

- **deepseek-r1:14b**: `handoff_to_execute`（旧tool名）を実際には呼ばず、言葉で
  「引き継ぎます」と説明するだけで終わることが3回中3回発生。判断はできているが、
  tool呼び出しという手続きに変換できていなかった。
- **llama3.1:8b**: tool呼び出し自体は構造化`tool_calls`で確実に返る。しかし
  「やぁ」のような雑談にまでtoolを呼んでしまう誤検知が、temperatureを0まで
  下げても直らなかった（tools有りだと何か呼ばなければと思い込む癖）。
  toolを1個に絞っても改善せず、むしろ全メッセージで誤発火するようになり悪化した。
- **qwen2.5-coder:14b**: 構造化`tool_calls`は返さないが、`tool_call_from_content()`の
  フォールバックで拾える形のJSONを出す。プロンプトを強化（「toolは例外処理」
  「rememberは価値ある事実がある時だけ」を明記）した上で検証したところ、
  雑談には反応せず・曖昧な依頼は聞き返し・明確な依頼は正しく引き継ぐ、と
  最もバランスが良かった。現在の既定モデル。

Execute役の既定モデルも同じ qwen2.5-coder:14b。これは `roles/execute/role.json`の
`"model"`が唯一の情報源で、引き継ぎ時に使うモデルも`scripts.dispatch.invoke_role()`が
そこから読む。
[NOTE] 以前は`chat_agent.py`が引き継ぎ先のモデルを`scripts.config.MODELS["6"]`という
別の決め打ちから取っており、`roles/execute/role.json`の`"model"`を書き換えても
実際の引き継ぎ先モデルは変わらないという不整合があった（役の定義が2箇所に分かれていた）。
`invoke_role()`が`role.json`だけを見るようにして解消した。
`roles/execute/prompt.txt` の手本セクションが「→ tool(args) を呼ぶ。」という擬似コード表記だった時は、
モデルがそれをそのまま文章として書き写すだけでtoolを呼ばない不具合があった。
手本は「実際にtool呼び出し機能を使う」ことを明記し、コピー可能な疑似コードを避ける形に直した。

[WARNING] 小型ローカルモデルは判断を誤ることがある（例: 相対パスでのファイル読み込みに
1回失敗しただけで「ファイルが存在しない」と誤った結論をremember/return_to_callerに
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

### モジュール共用時のtool制限漏れ（修正済み）

Review役を追加する際に発見。`arc_agent.py`は元々Execute役専用に書かれており、
モジュールの先頭で `ROLE = load_role(BASE_DIR, "execute")` と一度だけ読み込んで
いた。Review役が同じ`arc_agent.py`を`module`として指定しても、関数内部が
このモジュール直下の`ROLE`（execute固定）を参照している限り、実際に使われる
プロンプトとtool一覧はexecuteのものになってしまい、Review役の`role.json`で
`edit_file`を外していても意味が無い（読み取り専用のつもりが編集可能なまま）
という事故になるところだった。
`start_interactive_chat()`に`role_id`引数を追加し、渡された場合はモジュール直下の
`ROLE`ではなく`load_role(BASE_DIR, role_id)`を都度読み込んで使うように修正。
`scripts/dispatch.py`の`invoke_role()`が常に`role_id`を渡すため、以後この種の
役は安全に追加できる。

---

## ファイル構成

```
tools/agent/
├── chat_agent.py            雑談役 本体・プログラムの入口（ユーザーが起動するのはこれ）
├── arc_agent.py              Execute役 本体（scripts.dispatch経由で動的に呼ばれる）
├── README.md                    このファイル
├── roles/
│   ├── chat/
│   │   ├── role.json             雑談役の定義（モデル・専門性・使うtool名・引き継ぎ先）
│   │   └── prompt.txt            雑談役システムプロンプト
│   ├── execute/
│   │   ├── role.json             Execute役の定義（モデル・専門性・使うtool名・実装module）
│   │   └── prompt.txt            Execute役システムプロンプト
│   └── review/
│       ├── role.json             Review役の定義（Execute役と同じmoduleだがtoolsを読み取り専用に絞る）
│       └── prompt.txt            Review役システムプロンプト
└── scripts/
    ├── config.py               定数（ファイル名等）
    ├── ollama.py               Ollamaサーバーのライフサイクル管理（起動・停止・VRAM解放）
    ├── tools.py                  tool定義・TOOL_REGISTRY・tool_calls⇔内部アクション形式の変換
    ├── display.py                 ストリーム応答の表示（Spinner・思考中/出力中表示）。役をまたいで共通
    ├── role_loader.py            roles/ からの役の読み込み。専門性・引き継ぎ先の解決も担う
    ├── dispatch.py                役の入れ子呼び出しの共通化（role.jsonの"module"を動的import）
    └── memory.py                 セッションログ・共有メモ(shared_memory.md)の読み書き
```
