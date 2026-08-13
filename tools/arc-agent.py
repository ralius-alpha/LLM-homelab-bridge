import os
import sys
import time
import subprocess
import shutil
import re
import json
import unicodedata
import urllib.request
import urllib.error
from datetime import datetime

# ==========================================
# 1. 動作・環境設定 (Intel Arc A770 最適化)
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPT_FILE = os.path.join(BASE_DIR, "prompt.txt")
OLLAMA_HOST = "http://127.0.0.1:11434"

MODELS = {
    "1": ("DeepSeek-R1 : 7B", "deepseek-r1:7b"),
    "2": ("DeepSeek-R1 : 14B", "deepseek-r1:14b"),
    "3": ("DeepSeek-R1 : 32B", "deepseek-r1:32b"),
    "4": ("DeepSeek-R1 : 1.5B", "deepseek-r1:1.5b")
}

# 承認モード定義
EXEC_MODES = {
    "safe": "Safe Auto (参照系は自動実行 / 変更・削除は要承認)",
    "strict": "Strict (全コマンドで事前承認が必要)",
    "full": "Full Auto (全コマンドを自動実行)"
}

CURRENT_MODE = "safe"  # デフォルトモード

def load_system_prompt():
    """外部テキストファイルから初期プロンプトを読み込む"""
    if os.path.exists(PROMPT_FILE):
        with open(PROMPT_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    return "すべて日本語で回答してください。"

def setup_environment():
    """Intel Arc A770 用の環境変数をシステムプロセスに適用"""
    os.environ["OLLAMA_DEBUG"] = "1"
    os.environ["OLLAMA_NUM_PARALLEL"] = "1"
    
    # Intel GPU高速化フラグ (Immediate Command Listsの有効化が最も重要)
    os.environ["SYCL_PI_LEVEL_ZERO_USE_IMMEDIATE_COMMANDLISTS"] = "1"
    os.environ["SYCL_CACHE_PERSISTENT"] = "1"
    os.environ["SYCL_ENABLE_DEFAULT_CONTEXTS"] = "1"
    
    # 注意: UHD 630が存在するため、A770が 1 になる可能性があります。
    # 速度が出ない場合は "level_zero:1" に変更して検証してください。
    os.environ["ONEAPI_DEVICE_SELECTOR"] = "level_zero:0"
    os.environ["OLLAMA_GPU_OVERHEAD"] = "1024"
    
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    ollama_path = os.path.join(local_app_data, "Programs", "Ollama")
    if os.path.exists(ollama_path):
        os.environ["PATH"] = f"{ollama_path};{os.environ['PATH']}"

def cleanup_processes():
    """裏で固まっている古いOllamaプロセスを完全に掃除"""
    if shutil.which("taskkill"):
        subprocess.run("taskkill /f /im ollama.exe", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)

def wait_for_server(timeout=20):
    """サーバーの正常起動をヘルスチェックAPIで確認"""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            with urllib.request.urlopen(OLLAMA_HOST, timeout=1) as response:
                if response.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False

def run_server(log_file):
    """バックグラウンドでOllamaサーバーを起動"""
    print("[SERVER] サーバー起動中...")
    if os.path.exists(log_file):
        try: os.remove(log_file)
        except OSError: pass
            
    f_log = open(log_file, "w", encoding="utf-8")
    server_proc = subprocess.Popen(["ollama", "serve"], stdout=f_log, stderr=subprocess.STDOUT, text=True)
    
    if not wait_for_server():
        print("[ERROR] サーバーの起動確認にタイムアウトしました。ログを確認してください。")
    else:
        print("[SERVER] サーバー起動完了。")
    return server_proc

def warmup_model(model_name):
    """モデルのVRAMロードと初期プロンプトの解読を監視"""
    print("[INFO] 初期プロンプト注入中...")
    _ = load_system_prompt()
    time.sleep(0.5)

    print("[INFO] 初期プロンプト解読中（AI）...")
    try:
        # VRAMからのモデル解放を防ぐため keep_alive を追加
        payload = json.dumps({
            "model": model_name,
            "keep_alive": -1
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{OLLAMA_HOST}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=120) as res:
            pass
        print("[INFO] 解読完了。準備が整いました。")
    except Exception as e:
        print(f"[WARN] モデルロード中にエラーが発生しましたが続行します: {e}")

def extract_command(response_text):
    """レスポンスからEXECUTE_COMMANDブロックを抽出"""
    pattern = r"\[EXECUTE_COMMAND\]\s*(.*?)\s*\[/EXECUTE_COMMAND\]"
    matches = re.findall(pattern, response_text, re.DOTALL)
    if matches:
        for raw_cmd in matches:
            clean_lines = [
                line.strip() for line in raw_cmd.splitlines() 
                if line.strip() and not line.strip().startswith('#')
            ]
            if clean_lines:
                sanitized_cmd = " ; ".join(clean_lines)
                if "[EXECUTE_COMMAND]" not in sanitized_cmd:
                    return sanitized_cmd
    return None

def is_read_only_command(command: str) -> bool:
    """コマンドが可逆（参照系）か不可逆かを判定"""
    cmd = command.strip()
    
    # ファイル書き込みリダイレクト (>, >>) があれば不可逆と判定
    if re.search(r'>|>>', cmd):
        return False
        
    # 明示的な破壊・変更操作（PS動詞 / 固有コマンド）
    mutating_patterns = [
        r'\b(remove|set|new|add|rename|move|copy|clear|stop|restart|invoke|start|register|unregister)-',
        r'\b(rm|del|erase|mkdir|md|rmdir|rd|mv|cp|ren|write|out-file|set-content|add-content|clear-content)\b',
        r'\bgit\s+(commit|push|pull|checkout|merge|rebase|reset|clean|branch\s+-[dD])\b',
        r'\b(pip|npm|yarn|cargo|apt|winget|choco)\s+(install|uninstall|update|remove|build)\b'
    ]
    
    for pattern in mutating_patterns:
        if re.search(pattern, cmd, re.IGNORECASE):
            return False
            
    # 可逆（参照・読み取り）操作パターン
    read_only_patterns = [
        r'^\s*(get|show|find|test|select|measure)-',
        r'^\s*(dir|ls|cat|type|pwd|cd|tree|echo|Get-ChildItem|Get-Content|Get-Location|Select-String)\b',
        r'^\s*git\s+(status|log|diff|show|branch)\b'
    ]
    
    # セミコロン分割されたサブコマンドを個別にチェック
    sub_commands = [c.strip() for c in cmd.split(';') if c.strip()]
    for sub in sub_commands:
        sub_is_ro = False
        for pattern in read_only_patterns:
            if re.search(pattern, sub, re.IGNORECASE):
                sub_is_ro = True
                break
        if not sub_is_ro:
            return False
            
    return True

def execute_system_command_passthrough(command: str, mode: str) -> str:
    """PowerShell上でコマンドを実行し、結果を返却"""
    read_only = is_read_only_command(command)
    op_type_str = "可逆(参照系)" if read_only else "不可逆(変更/削除系)"
    
    print(f"\n[⚠️ API TRIGGERED] コマンド要求: {command}")
    print(f"[OP TYPE] 操作属性: {op_type_str}")

    # 承認が必要かどうかの判定
    need_approval = False
    if mode == "strict":
        need_approval = True
    elif mode == "safe":
        need_approval = not read_only
    elif mode == "full":
        need_approval = False

    if need_approval:
        raw_confirm = input(f"この {op_type_str} コマンドを実行しますか？ (y/n): ")
        confirm = unicodedata.normalize('NFKC', raw_confirm).strip().lower()
        if confirm != 'y':
            print("[API NOTICE] ユーザーによってコマンドの実行が拒否されました。")
            return "[SYSTEM NOTICE] ユーザーによってコマンドの実行が拒否されました。"

    print(f"[RUNNING] -> {command}")
    try:
        escaped_cmd = command.replace('"', '\"')
        ps_command = f'powershell -NoProfile -ExecutionPolicy Bypass -Command "{escaped_cmd}"'
        result = subprocess.run(ps_command, shell=True, capture_output=True)
        
        try:
            stdout = result.stdout.decode('utf-8')
            stderr = result.stderr.decode('utf-8')
        except UnicodeDecodeError:
            stdout = result.stdout.decode('cp932', errors='replace')
            stderr = result.stderr.decode('cp932', errors='replace')
            
        print("--- [COMMAND OUTPUT] ---")
        if stdout: print(stdout, end="")
        if stderr: print(f"Error Output: {stderr}", end="")
        print("\n------------------------")

        output = ""
        if stdout: output += stdout
        if stderr: output += f"\n[Error Output]\n{stderr}"
        return output if output else "(実行結果: 出力なし)"
    except Exception as e:
        print(f"[API ERROR] コマンドの実行に失敗しました: {e}")
        return f"[API ERROR] 実行失敗: {e}"

def start_interactive_chat(model_name: str, exec_mode: str, debug_mode: bool = False):
    """対話セッション管理（debug_modeがTrueの場合、生データを全出力＆会話ログをファイルに記録）"""
    warmup_model(model_name)
    
    # ログファイルの準備（デバッグモード用）
    chat_log_file = None
    if debug_mode:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        chat_log_file = os.path.join(BASE_DIR, f"debug_chat_log_{timestamp}.txt")
        print(f"[DEBUG LOG] 会話ログ記録先: {chat_log_file}")
        with open(chat_log_file, "w", encoding="utf-8") as f:
            f.write(f"=== Debug Chat Log - Model: {model_name} - Time: {timestamp} ===\n\n")

    print(f"\n[CLIENT] {model_name} との対話セッションを開始します。")
    print(f"[MODE] 実行承認モード: {EXEC_MODES[exec_mode]}")
    if debug_mode:
        print("[MODE] ★デバッグモード有効（全生テキスト出力 ＆ ログ保存中）★")
    print("※ 終了するには 'exit' または 'quit' と入力してください。")
    print("※ 複数行入力対応です。送信するには新しい行で 'EOF' と入力するか、Ctrl+Z/Ctrl+Dを入力してください。\n")
    
    messages = [{"role": "system", "content": load_system_prompt()}]
    print("[AI] 初期化が完了しました。質問をどうぞ。")
    
    while True:
        try:
            print("\n[Input] ---------------------------------")
            lines = []
            while True:
                line = input(">>> " if not lines else "... ")
                if not lines and line.strip().lower() in ["exit", "quit"]:
                    return
                if line.strip().upper() == "EOF":
                    break
                lines.append(line)
        except (EOFError, KeyboardInterrupt):
            print("\n[INFO] 対話を終了します。")
            return
            
        user_input = "\n".join(lines).strip()
        if not user_input:
            continue
            
        messages.append({"role": "user", "content": user_input})
        
        if chat_log_file:
            with open(chat_log_file, "a", encoding="utf-8") as f:
                f.write(f"\n[User Input]\n{user_input}\n")
        
        while True:
            # VRAM超過を防ぐため num_ctx を制限し、アンロードを防ぐ keep_alive を指定
            payload = json.dumps({
                "model": model_name,
                "messages": messages,
                "stream": True,
                "keep_alive": -1,
                "options": {
                    "num_ctx": 4096
                }
            }).encode("utf-8")
            
            req = urllib.request.Request(
                f"{OLLAMA_HOST}/api/chat",
                data=payload,
                headers={"Content-Type": "application/json"}
            )
            
            ai_response_full = ""
            print("\n--- DeepSeek Response (Raw Stream) ---" if debug_mode else "\n--- DeepSeek Response ---")
            
            try:
                with urllib.request.urlopen(req) as res:
                    for line in res:
                        if not line:
                            continue
                        
                        chunk = json.loads(line.decode("utf-8"))
                        content = chunk.get("message", {}).get("content", "")
                        ai_response_full += content
                        
                        # デバッグモード時は一切の隠蔽や置換をせず、受け取ったチャンクをそのまま出力
                        if content:
                            print(content, end="", flush=True)
                                
            except urllib.error.URLError as e:
                print(f"\n[ERROR] 通信エラー: {e}")
                return

            print()
            messages.append({"role": "assistant", "content": ai_response_full})
            
            if chat_log_file:
                with open(chat_log_file, "a", encoding="utf-8") as f:
                    f.write(f"\n[AI Response]\n{ai_response_full}\n")
            
            command_to_run = extract_command(ai_response_full)
            if command_to_run:
                cmd_result = execute_system_command_passthrough(command_to_run, exec_mode)
                messages.append({
                    "role": "user",
                    "content": f"[SYSTEM COMMAND OUTPUT for '{command_to_run}']\n{cmd_result}"
                })
                if chat_log_file:
                    with open(chat_log_file, "a", encoding="utf-8") as f:
                        f.write(f"\n[System Command Output]\n{cmd_result}\n")
                print("\n[SYSTEM] コマンド実行結果をAIにフィードバックして解析中...")
                continue
            else:
                break

def select_exec_mode() -> str:
    """承認モード選択画面"""
    global CURRENT_MODE
    os.system("cls" if os.name == "nt" else "clear")
    print("===================================================")
    print(" 承認モードの変更 (Execution Approval Mode)")
    print("===================================================")
    print(" [1] Safe Auto (推奨: 参照系は自動実行 / 変更・削除は事前承認)")
    print(" [2] Strict    (全コマンド実行時に事前承認が必要)")
    print(" [3] Full Auto (危険: 全コマンドを無確認で自動実行)")
    print("===================================================")
    choice = input("モード番号を選択してください (1-3): ").strip()
    if choice == "1":
        CURRENT_MODE = "safe"
    elif choice == "2":
        CURRENT_MODE = "strict"
    elif choice == "3":
        CURRENT_MODE = "full"
    return CURRENT_MODE

def main():
    global CURRENT_MODE
    log_file = os.path.join(BASE_DIR, "ollama_server.log")
    setup_environment()
    
    while True:
        cleanup_processes()
        os.system("cls" if os.name == "nt" else "clear")
        
        print("===================================================")
        print("   Official Ollama - Intel Arc A770 Agent UI")
        print("===================================================")
        for key, (label, _) in MODELS.items():
            print(f" [{key}] {label}")
        print(" [m] 承認モード変更")
        print(" [9] Debug Log & Raw Stream Mode (デバッグ用)")
        print(" [5] EXIT")
        print("===================================================")
        print(f" [Mode]     Current: {EXEC_MODES[CURRENT_MODE]}")
        print(f" [Log Path] {log_file}")
        print(f" [Prompt]   Loaded from '{PROMPT_FILE}'")
        print("===================================================")
        
        raw_choice = input("メニュー番号を入力してください (1-5 / 9 / m): ")
        choice = unicodedata.normalize('NFKC', raw_choice).strip().lower()
        
        if choice in MODELS:
            server_proc = run_server(log_file)
            _, model_name = MODELS[choice]
            
            start_interactive_chat(model_name, CURRENT_MODE, debug_mode=False)
            
            print("\n[INFO] セッションが終了しました。クリーンアップ中...")
            server_proc.terminate()
            server_proc.wait()
            cleanup_processes()
            input("\nメニューに戻るには何かキーを押してください...")
        elif choice == "9":
            # 9番: デバッグモード（モデル選択後にデバッグ用セッションへ移行）
            print("\n--- デバッグモード用モデル選択 ---")
            for key, (label, _) in MODELS.items():
                print(f" [{key}] {label}")
            sub_choice = input("モデル番号を選択してください (1-4): ").strip()
            if sub_choice in MODELS:
                server_proc = run_server(log_file)
                _, model_name = MODELS[sub_choice]
                
                start_interactive_chat(model_name, CURRENT_MODE, debug_mode=True)
                
                print("\n[INFO] デバッグセッションが終了しました。クリーンアップ中...")
                server_proc.terminate()
                server_proc.wait()
                cleanup_processes()
                input("\nメニューに戻るには何かキーを押してください...")
        elif choice == "m":
            select_exec_mode()
        elif choice == "5":
            cleanup_processes()
            print("[INFO] 終了しました。")
            break

if __name__ == "__main__":
    main()