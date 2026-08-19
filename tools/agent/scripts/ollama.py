# -*- coding: utf-8 -*-
"""
Ollama サーバーの管理モジュール。
サーバーの起動・停止・プロセス掃除・VRAM解放・ウォームアップを担当する。

[NOTE] 設定値（OLLAMA_HOST, KEEP_ALIVE）は config.py から読み込む。
"""

import os
import time
import json
import shutil
import subprocess
import urllib.request

from scripts.config import OLLAMA_HOST, KEEP_ALIVE


def setup_environment():
    """Intel Arc最適化のための環境変数を設定する（chat_agent.py / arc_agent.py共通）。"""
    os.environ["OLLAMA_DEBUG"] = "1"
    os.environ["OLLAMA_NUM_PARALLEL"] = "1"
    os.environ["SYCL_PI_LEVEL_ZERO_USE_IMMEDIATE_COMMANDLISTS"] = "1"
    os.environ["SYCL_CACHE_PERSISTENT"] = "1"
    os.environ["SYCL_ENABLE_DEFAULT_CONTEXTS"] = "1"
    os.environ["ONEAPI_DEVICE_SELECTOR"] = "level_zero:0"
    os.environ["OLLAMA_GPU_OVERHEAD"] = "1024"

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    ollama_path = os.path.join(local_app_data, "Programs", "Ollama")
    if os.path.exists(ollama_path):
        os.environ["PATH"] = f"{ollama_path};{os.environ['PATH']}"


def cleanup_processes():
    """Ollamaプロセスを完全に掃除する。"""
    if shutil.which("taskkill"):
        subprocess.run("taskkill /f /im ollama.exe", shell=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # [NOTE] 実際にモデルを保持しているランナープロセスは llama-server.exe という
        #        名前で、ollama_llama_server.exe ではない（検証時点のOllama 0.32.6）。
        #        これを見落とすと ollama.exe を殺してもランナーだけ孤児化して
        #        VRAM/メモリを掴んだまま残る。両方殺す。
        subprocess.run("taskkill /f /im llama-server.exe", shell=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run("taskkill /f /im ollama_llama_server.exe", shell=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run("taskkill /f /im \"ollama app.exe\"", shell=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)


def unload_all_models():
    """ロード中のモデルを keep_alive:0 でVRAMから即座に降ろす。"""
    try:
        req = urllib.request.Request(f"{OLLAMA_HOST}/api/ps")
        with urllib.request.urlopen(req, timeout=3) as res:
            data = json.loads(res.read().decode("utf-8"))
        loaded = data.get("models", [])
        if not loaded:
            return
        for m in loaded:
            name = m.get("name") or m.get("model")
            if not name:
                continue
            print(f"[UNLOAD] モデルをVRAMから解放中: {name}")
            payload = json.dumps({"model": name, "keep_alive": 0}).encode("utf-8")
            r = urllib.request.Request(
                f"{OLLAMA_HOST}/api/generate", data=payload,
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(r, timeout=10) as _:
                    pass
            except Exception:
                pass
        time.sleep(1)
    except Exception:
        # サーバーが既に落ちている等は無視
        pass


def startup_cleanup():
    """起動時に、既に動いているOllamaサーバー/プロセスがあれば全部掃除する。"""
    print("[STARTUP] 既存のOllamaプロセスを確認中...")
    found = False

    try:
        with urllib.request.urlopen(OLLAMA_HOST, timeout=1) as res:
            if res.status == 200:
                found = True
                print("[STARTUP] 既に起動中のOllamaサーバーを検出しました。")
    except Exception:
        pass

    if shutil.which("tasklist"):
        try:
            result = subprocess.run("tasklist", shell=True,
                                    capture_output=True, text=True)
            stdout_lower = (result.stdout or "").lower()
            if "ollama" in stdout_lower or "llama-server" in stdout_lower:
                found = True
        except Exception:
            pass

    if found:
        print("[STARTUP] 既存のOllamaプロセスを全て終了します...")
        cleanup_processes()
        time.sleep(1)
        print("[STARTUP] クリーンアップ完了。")
    else:
        print("[STARTUP] 起動中のOllamaはありません。そのまま続行します。")


def wait_for_server(timeout=20):
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
    print("[SERVER] サーバー起動中...")
    if os.path.exists(log_file):
        try:
            os.remove(log_file)
        except OSError:
            pass
    f_log = open(log_file, "w", encoding="utf-8")
    server_proc = subprocess.Popen(["ollama", "serve"], stdout=f_log,
                                   stderr=subprocess.STDOUT, text=True)
    if not wait_for_server():
        print("[ERROR] サーバーの起動確認にタイムアウトしました。")
    else:
        print("[SERVER] サーバー起動完了。")
    return server_proc


def warmup_model(model_name):
    print("[INFO] 初期プロンプト注入中...")
    time.sleep(0.5)
    print("[INFO] 初期プロンプト解読中（AI）...")
    try:
        payload = json.dumps({"model": model_name, "keep_alive": KEEP_ALIVE}).encode("utf-8")
        req = urllib.request.Request(
            f"{OLLAMA_HOST}/api/generate", data=payload,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as res:
            pass
        print("[INFO] 解読完了。準備が整いました。")
    except Exception as e:
        print(f"[WARN] モデルロード中にエラーが発生しましたが続行します: {e}")