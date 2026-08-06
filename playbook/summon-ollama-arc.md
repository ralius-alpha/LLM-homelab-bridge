# 召喚手順書: Intel Arc で Ollama を動かす

> Windows 11 + Intel Arc A770 マシンで、ローカルLLM（Ollama）を
> GPU アクセラレーション付きで動かすまでの実戦手順。
>
> 対象マシン: Arc A770 (16GB) / RAM 64GB / Core i7-10700K / Windows 11
> 方式: IPEX-LLM Ollama Portable Zip（環境構築不要の最短ルート）

---

## この手順書の前提

- Intel Arc は素の Ollama では GPU を使えない
- Intel 提供の IPEX-LLM 経由が必須
- Portable Zip 方式なら oneAPI や Python 環境構築が不要（依存同梱）
- パスに日本語・空白・ドットを含めない（トラブル回避）

[NOTE] うまくいかない時は末尾の「トラブル対処」を見る。
Arc は詰まりやすいが、大半は既知のパターンで解決できる。

---

## ステップ0: GPUドライバの確認・更新（最重要）

ここが古いと後続が全部失敗する。最初に必ず確認する。

1. スタートメニューから「インテル グラフィックス・ソフトウェア」を開く
   （Intel Graphics Software）
2. ドライバのバージョンを確認する
3. 推奨: 32.0.101.6078 以降（できれば最新）
4. 古ければ更新 → PC再起動

- 「インテル グラフィックス・ソフトウェア」が無い場合は、
  Intel公式サイトから「Intel Arc Graphics Driver」を入れる

チェック:

- [ ] ドライバ確認・更新済み
- [ ] 更新した場合は再起動済み

---

## ステップ1: Ollama Portable Zip をダウンロード

1. ブラウザで以下を開く
   https://github.com/intel/ipex-llm/releases
2. Windows 向けの Ollama Portable Zip を探す
   - ファイル名の形: `ollama-ipex-llm-2.3.0bXXXXXXXX-win.zip`
   - バージョン番号は最新のものを選ぶ
3. その zip をダウンロード

推奨: 2.3.0b 以降（Ollama v0.9.3+ 同梱、Qwen2.5 / DeepSeek-R1 対応）

チェック:

- [ ] win.zip をダウンロードした

---

## ステップ2: 展開

パスをシンプルに保つのが安全。

1. ダウンロードした zip を展開
2. 展開先はシンプルなパスにする

推奨展開先:

```text
C:\ollama-ipex
```

[WARNING] `C:\Users\（ユーザー名）\...` のようにユーザー名や
ドット・日本語・空白を含むパスは避ける。稀に不具合の原因になる。

チェック:

- [ ] C:\ollama-ipex（等）に展開した

---

## ステップ3: 初期化（初回のみ）

展開フォルダ内に `init-ollama.bat` があれば、先に1回実行する。

1. 展開フォルダ `C:\ollama-ipex` を開く
2. `init-ollama.bat` があればダブルクリックで実行
   （無ければこのステップはスキップ）

チェック:

- [ ] init-ollama.bat を実行した（存在する場合）

---

## ステップ4: サーバー起動

1. 展開フォルダ内の `start-ollama.bat` を実行
   （ダブルクリック、または cmd から実行）
2. 新しいウィンドウが開き、Ollama サーバーが起動する
3. IPEX-LLM アクセラレーション付きで動く

[IMPORTANT] このサーバーウィンドウは開いたままにする。
閉じるとサーバーが止まる。

チェック:

- [ ] start-ollama.bat を実行し、サーバーウィンドウが起動した
- [ ] サーバーウィンドウは開いたまま維持している

---

## ステップ5: 最初のモデルで対話（召喚の瞬間）

別のコマンドプロンプトを使う。

1. 新しいコマンドプロンプトを開く（ステップ4とは別ウィンドウ）
2. 展開フォルダに移動

   ```cmd
   cd C:\ollama-ipex
   ```

3. まず軽めの 8B モデルで動作確認

   ```cmd
   ollama.exe run llama3.1:8b
   ```

4. モデルのダウンロードが始まる（初回のみ、数GB）
5. 完了すると対話プロンプトが出る
6. 日本語で話しかけてみる

   例: 「こんにちは。あなたは何ができますか?」

チェック:

- [ ] モデルのダウンロードが完了した
- [ ] 日本語で応答が返ってきた（召喚成功）

---

## ステップ6: GPU が使われているか確認（重要）

CPU で動いていないか確認する。ここが召喚成功の本当の確認。

1. タスクマネージャーを開く（Ctrl + Shift + Esc）
2. 「パフォーマンス」タブ
3. GPU の項目で **Arc A770 の方**（GPU 0 or 1）を選ぶ
4. 対話中に「Compute」または「XPU」の負荷が上がるか確認

- 負荷が上がる → GPU 稼働。召喚完全成功
- 0% のまま → CPU で動作中（トラブル対処へ）

[WARNING] このマシンには GPU が2つある。
  - Arc A770 Graphics（これを使いたい）
  - UHD Graphics 630（CPU内蔵、使わない）
必ず Arc A770 の負荷を見る。内蔵GPUと間違えない。

チェック:

- [ ] Arc A770 の Compute/XPU 負荷が上がるのを確認した

---

## ステップ7: 実用モデルを試す（任意）

動作確認できたら、日本語実用モデルを試す。

```cmd
ollama.exe run qwen2.5:14b
```

A770 16GB のスイートスポット。日本語性能が高い。

さらに高性能を狙うなら（RAM64GBを活かす）:

```cmd
ollama.exe run qwen2.5:32b
```

推奨モデル早見:

| モデル | 用途 | 目安 |
|--------|------|------|
| `llama3.1:8b` | 動作確認 | 非常に高速 |
| `qwen2.5:14b` | 日本語・実用 | スイートスポット |
| `qwen2.5:32b` | 高性能狙い | RAM活用で動作 |

チェック:

- [ ] qwen2.5:14b で日本語対話を確認した（任意）

---

## トラブル対処（Arc あるある）

### 症状: `sycl8.dll が見つからない`

古い 2024 版の残骸が原因。

対処:
1. 展開フォルダを完全に削除
2. 最新の Portable Zip を再ダウンロード・再展開

### 症状: GPU 負荷が 0%（CPU で動いている）

Arc が選ばれていない。

対処: `start-ollama.bat` を実行する前に、同じ cmd で環境変数を設定してから起動する。

```cmd
set ONEAPI_DEVICE_SELECTOR=level_zero:0
set OLLAMA_NUM_GPU=999
set ZES_ENABLE_SYSMAN=1
start-ollama.bat
```

- `ONEAPI_DEVICE_SELECTOR=level_zero:0`: ディスクリートGPU（Arc）を強制指定
- `OLLAMA_NUM_GPU=999`: できる限りGPUにオフロード
- `ZES_ENABLE_SYSMAN=1`: GPU管理機能を有効化

### 症状: 内蔵GPU（UHD 630）が使われる

上記の `ONEAPI_DEVICE_SELECTOR=level_zero:0` で Arc を指定する。
それでもダメなら `level_zero:1` も試す（環境により番号が違う場合がある）。

### 症状: モデルのダウンロードが遅い

ミラーを使う。`start-ollama.bat` 起動前に設定。

```cmd
set OLLAMA_MODEL_SOURCE=modelscope
```

### 症状: コンテキスト長を増やしたい

デフォルトは 2048。増やす場合。

```cmd
set OLLAMA_NUM_CTX=16384
```

### 症状: パフォーマンスを上げたい（Arc向け最適化）

```cmd
set SYCL_PI_LEVEL_ZERO_USE_IMMEDIATE_COMMANDLISTS=1
```

---

## 召喚後のAPI確認（次の布石）

Ollama サーバーが起動していれば、API が使える状態になっている。
別PC（Mac等）から接続する布石として、まずローカルで確認。

ブラウザで以下を開く:

```text
http://localhost:11434
```

「Ollama is running」と表示されれば、API が生きている。

[NOTE] この 11434 ポートが、後で VSCode 拡張（Continue / Cline）や
Mac からの接続で使う入り口になる。今はローカルで動作確認まででOK。

---

## 完了チェックリスト

- [ ] ステップ0: ドライバ確認・更新
- [ ] ステップ1: Portable Zip ダウンロード
- [ ] ステップ2: 展開
- [ ] ステップ3: 初期化（初回のみ）
- [ ] ステップ4: サーバー起動
- [ ] ステップ5: モデルで日本語対話（召喚）
- [ ] ステップ6: Arc GPU 稼働を確認
- [ ] ステップ7: 実用モデル（任意）
- [ ] API 確認（localhost:11434）

全部埋まれば召喚完了。次は VSCode 拡張との接続・リポジトリアクセス設定へ。

---

## 参考リンク

- IPEX-LLM GitHub: https://github.com/intel/ipex-llm
- Ollama Portable Zip Quickstart:
  https://github.com/intel/ipex-llm/blob/main/docs/mddocs/Quickstart/ollama_portable_zip_quickstart.md