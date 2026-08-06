#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
マルチOS スペック確認スクリプト

Windows / macOS / Linux に対応し、以下を標準出力に表示する。
  - OS 種別・バージョン
  - CPU モデル名・コア数
  - RAM 総容量
  - GPU モデル名
  - VRAM 容量
  - ストレージ空き容量
  - Python バージョン

依存: 標準ライブラリのみ（pip インストール不要）
用途: ローカルLLM 環境の下調べ。数値を確認してコピペする。

[NOTE] VRAM の自動検出は OS・GPUベンダーによって方法が異なる。
取得できない場合は「手動で確認してください」と案内する。
"""

import platform
import subprocess
import shutil
import os
import sys


def run_cmd(cmd):
    """コマンドを実行し、標準出力の文字列を返す。失敗時は None。"""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except Exception:
        return None


def human_bytes(n):
    """バイト数を人間が読みやすい単位に変換する。"""
    if n is None:
        return "不明"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    for unit in units:
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


# ==========================================================
# OS 情報
# ==========================================================
def get_os_info():
    system = platform.system()  # 'Windows' / 'Darwin' / 'Linux'
    release = platform.release()
    version = platform.version()
    machine = platform.machine()  # 'x86_64' / 'arm64' 等

    os_name = {
        "Windows": "Windows",
        "Darwin": "macOS",
        "Linux": "Linux",
    }.get(system, system)

    return {
        "system": system,
        "name": os_name,
        "release": release,
        "version": version,
        "arch": machine,
    }


# ==========================================================
# CPU 情報
# ==========================================================
def get_cpu_info(system):
    model = "不明"
    cores = os.cpu_count() or "不明"

    if system == "Darwin":
        out = run_cmd(["sysctl", "-n", "machdep.cpu.brand_string"])
        if out:
            model = out
    elif system == "Linux":
        # /proc/cpuinfo から model name を拾う
        try:
            with open("/proc/cpuinfo", "r") as f:
                for line in f:
                    if "model name" in line:
                        model = line.split(":", 1)[1].strip()
                        break
        except Exception:
            pass
    elif system == "Windows":
        out = run_cmd(["wmic", "cpu", "get", "name"])
        if out:
            lines = [l.strip() for l in out.splitlines() if l.strip()]
            if len(lines) >= 2:
                model = lines[1]
        # wmic が無い環境（新しめのWindows）では PowerShell を試す
        if model == "不明":
            out = run_cmd([
                "powershell", "-NoProfile", "-Command",
                "(Get-CimInstance Win32_Processor).Name"
            ])
            if out:
                model = out.splitlines()[0].strip()

    return {"model": model, "cores": cores}


# ==========================================================
# RAM 情報
# ==========================================================
def get_ram_info(system):
    total = None

    if system == "Darwin":
        out = run_cmd(["sysctl", "-n", "hw.memsize"])
        if out and out.isdigit():
            total = int(out)
    elif system == "Linux":
        try:
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        # 単位は kB
                        kb = int(line.split()[1])
                        total = kb * 1024
                        break
        except Exception:
            pass
    elif system == "Windows":
        out = run_cmd([
            "wmic", "ComputerSystem", "get", "TotalPhysicalMemory"
        ])
        if out:
            lines = [l.strip() for l in out.splitlines() if l.strip()]
            if len(lines) >= 2 and lines[1].isdigit():
                total = int(lines[1])
        if total is None:
            out = run_cmd([
                "powershell", "-NoProfile", "-Command",
                "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory"
            ])
            if out and out.strip().isdigit():
                total = int(out.strip())

    return {"total": total}


# ==========================================================
# GPU / VRAM 情報
# ==========================================================
def get_gpu_info(system, arch):
    """
    GPU モデルと VRAM を取得する。
    複数の手段を順に試し、取れたものを返す。
    取得できない場合は手動確認を促す。
    """
    gpus = []  # [{"name": ..., "vram": ...(bytes or None)}]

    # --- まず NVIDIA を試す（全OS共通で nvidia-smi があれば強い） ---
    if shutil.which("nvidia-smi"):
        out = run_cmd([
            "nvidia-smi",
            "--query-gpu=name,memory.total",
            "--format=csv,noheader,nounits",
        ])
        if out:
            for line in out.splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 2:
                    name = parts[0]
                    # memory.total は MiB 単位
                    try:
                        vram = int(float(parts[1])) * 1024 * 1024
                    except ValueError:
                        vram = None
                    gpus.append({"name": name, "vram": vram})
            if gpus:
                return gpus

    # --- OS 別のフォールバック ---
    if system == "Darwin":
        # Apple Silicon はユニファイドメモリ（RAM と VRAM 共有）
        out = run_cmd([
            "system_profiler", "SPDisplaysDataType"
        ])
        if out:
            name = "不明"
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("Chipset Model:"):
                    name = line.split(":", 1)[1].strip()
                    break
            note = None
            if arch == "arm64":
                note = "Apple Silicon: VRAMはRAMと共有（ユニファイドメモリ）"
            gpus.append({"name": name, "vram": None, "note": note})
            return gpus

    elif system == "Linux":
        # Intel / AMD 等。lspci で名前だけでも拾う
        out = run_cmd(["lspci"])
        if out:
            for line in out.splitlines():
                low = line.lower()
                if "vga" in low or "3d controller" in low or "display" in low:
                    # "01:00.0 VGA compatible controller: ..." の後半を拾う
                    name = line.split(":", 2)[-1].strip()
                    gpus.append({"name": name, "vram": None})
            if gpus:
                return gpus

    elif system == "Windows":
        # 名前と VRAM を wmic で試す（AdapterRAM は4GB超で不正確な場合あり）
        out = run_cmd([
            "wmic", "path", "win32_VideoController",
            "get", "Name,AdapterRAM"
        ])
        if out:
            lines = [l.strip() for l in out.splitlines() if l.strip()]
            for line in lines[1:]:  # 1行目はヘッダ
                # 末尾の数値が AdapterRAM、それ以外が Name
                tokens = line.rsplit(None, 1)
                if len(tokens) == 2 and tokens[1].isdigit():
                    name = tokens[0].strip()
                    ram = int(tokens[1])
                    # AdapterRAM は 4GB を超えると 4294967295 で頭打ちになる既知の不具合
                    vram = ram if ram < 4294967295 else None
                    gpus.append({"name": name, "vram": vram})
                elif line:
                    gpus.append({"name": line, "vram": None})
            if gpus:
                return gpus
        # PowerShell フォールバック（名前のみ）
        out = run_cmd([
            "powershell", "-NoProfile", "-Command",
            "(Get-CimInstance Win32_VideoController).Name"
        ])
        if out:
            for line in out.splitlines():
                if line.strip():
                    gpus.append({"name": line.strip(), "vram": None})
            if gpus:
                return gpus

    # 何も取れなかった
    return gpus


# ==========================================================
# ストレージ情報
# ==========================================================
def get_storage_info():
    try:
        usage = shutil.disk_usage(os.path.abspath(os.sep))
        return {"free": usage.free, "total": usage.total}
    except Exception:
        return {"free": None, "total": None}


# ==========================================================
# 出力
# ==========================================================
def main():
    line = "=" * 50

    os_info = get_os_info()
    cpu_info = get_cpu_info(os_info["system"])
    ram_info = get_ram_info(os_info["system"])
    gpu_info = get_gpu_info(os_info["system"], os_info["arch"])
    storage_info = get_storage_info()

    print(line)
    print(" マシンスペック確認結果")
    print(line)

    print("\n[OS]")
    print(f"  種別      : {os_info['name']}")
    print(f"  リリース  : {os_info['release']}")
    print(f"  アーキ    : {os_info['arch']}")

    print("\n[CPU]")
    print(f"  モデル    : {cpu_info['model']}")
    print(f"  コア数    : {cpu_info['cores']}")

    print("\n[RAM]")
    print(f"  総容量    : {human_bytes(ram_info['total'])}")

    print("\n[GPU / VRAM]")
    if not gpu_info:
        print("  GPU       : 自動検出できませんでした")
        print("  [NOTE] 手動で確認してください:")
        print("    Windows : タスクマネージャー > パフォーマンス > GPU > 専用GPUメモリ")
        print("    macOS   : このMacについて（Apple Silicon はRAMと共有）")
        print("    Linux   : nvidia-smi / intel_gpu_top / lspci 等")
    else:
        for i, gpu in enumerate(gpu_info):
            idx = f"#{i+1}" if len(gpu_info) > 1 else ""
            print(f"  GPU{idx}     : {gpu.get('name', '不明')}")
            vram = gpu.get("vram")
            if vram:
                print(f"  VRAM{idx}    : {human_bytes(vram)}")
            else:
                print(f"  VRAM{idx}    : 自動検出できませんでした（手動確認を推奨）")
            if gpu.get("note"):
                print(f"  [NOTE]    : {gpu['note']}")

    print("\n[ストレージ（システムドライブ）]")
    print(f"  空き容量  : {human_bytes(storage_info['free'])}")
    print(f"  総容量    : {human_bytes(storage_info['total'])}")

    print("\n[Python]")
    print(f"  バージョン: {platform.python_version()}")

    print("\n" + line)
    print(" 確認のポイント")
    print(line)
    print("  - RAM と VRAM が、動かせるモデルのサイズを左右します")
    print("  - VRAM が自動検出できない場合は手動で確認してください")
    print("  - ストレージ空き容量はモデルのダウンロードに必要です")
    print("    （目安: 小型3B〜7Bで数GB、大型で数十GB）")
    print(line)


if __name__ == "__main__":
    main()