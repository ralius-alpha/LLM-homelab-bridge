# -*- coding: utf-8 -*-
"""
設定値（定数）を集約するモジュール。
実行時に書き換わらない、純粋な設定だけをここに置く。

[NOTE] BASE_DIR / PROMPT_FILE / WORK_DIR / CURRENT_MODE は
       実行時に決まる・書き換わる値なので、ここには置かず本体に残す。
"""

# Ollama サーバーのホスト
OLLAMA_HOST = "http://127.0.0.1:11434"

# アイドル時に VRAM を解放するまでの時間
KEEP_ALIVE = "3m"

# [NOTE] 各役(chat/execute)が使うモデルは roles/<role>/role.json 側で定義する
#        (scripts/role_loader.py)。モデル選定の経緯はREADME.md参照。

# 各セッションの会話ログを置くディレクトリ名
LOGS_DIRNAME = "logs"

# 全役共通の「忘れてはいけない事項」を置くファイル名
SHARED_MEMORY_FILENAME = "shared_memory.md"

# 選択可能なモデル一覧
MODELS = {
    "1": ("DeepSeek-R1 : 7B", "deepseek-r1:7b"),
    "2": ("DeepSeek-R1 : 14B", "deepseek-r1:14b"),
    "3": ("DeepSeek-R1 : 32B", "deepseek-r1:32b"),
    "4": ("DeepSeek-R1 : 1.5B", "deepseek-r1:1.5b(debug用軽量model)"),
    "5": ("DeepSeek-Coder-V2 : 16B (コード特化)", "deepseek-coder-v2:16b"),
    "6": ("Qwen2.5-Coder : 14B (コード特化・推奨)", "qwen2.5-coder:14b"),
    "7": ("Qwen2.5-Coder : 32B Q3 (最強・要VRAM)", "qwen2.5-coder:32b-instruct-q3_K_M")
}

# 承認モードの説明
EXEC_MODES = {
    "safe": "Safe Auto (参照系は自動実行 / 変更・削除は要承認)",
    "strict": "Strict (全コマンドで事前承認が必要)",
    "full": "Full Auto (全コマンドを自動実行)"
}

# message内で無視するノイズフィールド（毎チャンク付いてくるだけ）
IGNORE_FIELDS = {"role"}

# 思考としてカウント/表示する候補フィールド名
THINKING_FIELDS = {"thinking", "reasoning", "reasoning_content"}

# 点字スピナー（1秒で1周 = 10コマ×0.1秒）
SPINNER_FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']