"""Windows-friendly local launcher with visible output + persistent log.

No remote service or commercial data is used here. The web app remains local.
"""
from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from urllib.request import urlopen
import webbrowser

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "RUN_LOG.txt"
PORT = int(os.environ.get("PORT", "8765"))
URL = f"http://127.0.0.1:{PORT}"


def occupied() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.75):
            return True
    except OSError:
        return False


def open_when_ready(proc: subprocess.Popen) -> None:
    if os.environ.get("NO_BROWSER") == "1":
        return
    for _ in range(60):
        if proc.poll() is not None:
            return
        try:
            with urlopen(URL, timeout=0.5) as res:
                if res.status == 200:
                    webbrowser.open(URL)
                    return
        except Exception:
            pass
        time.sleep(0.5)


def main() -> int:
    print(f"数据服务地址：{URL}", flush=True)
    if occupied():
        msg = f"端口 {PORT} 已被占用。请先关闭旧版本服务窗口，或设置其他 PORT。"
        print("[无法启动] " + msg, flush=True)
        LOG.write_text(msg + "\n", encoding="utf-8")
        return 2
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PORT"] = str(PORT)
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", "app.py"], cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace", bufsize=1,
        )
    except Exception as exc:
        LOG.write_text(f"启动 Python 子进程失败：{exc}\n", encoding="utf-8")
        print("[无法启动]", exc, flush=True)
        return 3
    thread = threading.Thread(target=open_when_ready, args=(proc,), daemon=True)
    thread.start()
    print("正在启动 Web 服务；首次访问将在服务就绪后自动打开浏览器。", flush=True)
    try:
        with LOG.open("w", encoding="utf-8") as fp:
            fp.write(f"启动参数: {sys.executable} app.py; {URL}\n")
            fp.flush()
            assert proc.stdout is not None
            for line in proc.stdout:
                print(line, end="", flush=True)
                fp.write(line)
                fp.flush()
            rc = proc.wait()
            fp.write(f"\n进程结束，退出码: {rc}\n")
            fp.flush()
    except KeyboardInterrupt:
        print("\n用户已要求停止服务器。", flush=True)
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        return 0
    except Exception as exc:
        print("[启动器异常]", exc, flush=True)
        try:
            proc.terminate()
        except Exception:
            pass
        return 4
    if rc != 0:
        print(f"[服务异常退出] 退出码 {rc}，请把 RUN_LOG.txt 发给我。", flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
