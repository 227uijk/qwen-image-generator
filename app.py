#!/usr/bin/env python3
"""Qwen-Image 本地生图小工具：浏览器界面 + stable-diffusion.cpp 后端，只用标准库。"""

import base64
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

PORT = int(os.environ.get("QWEN_PORT", "7861"))
ROOT = Path(__file__).resolve().parent
BIN = ROOT / "bin"
MODELS = ROOT / "models"
OUT = ROOT / "outputs"
TMP = ROOT / "tmp"
HISTORY = ROOT / "history.json"
SETTINGS = ROOT / "settings.json"
DOWNLOADS = ROOT / "downloads.json"
for d in (BIN, MODELS, OUT, TMP):
    d.mkdir(exist_ok=True)

ROLES = {"dit": "生图模型", "te": "文本编码器", "vae": "VAE", "vision": "视觉组件",
         "engine": "sd.cpp 程序", "mlx": "MLX 模型包（整个仓库）", "mlx_engine": "MLX-Serve 程序",
         "other": "其他（仅下载）"}
MODEL_EXT = (".gguf", ".safetensors")

# 推荐预设：只是帮忙填好链接，用户也可以自己贴任意链接
HF = "https://huggingface.co"
PRESETS = [
    {"name": "Qwen-Image 2.1 基础套装", "desc": "sd.cpp 程序 + 4bit 模型 + VAE + 视觉组件，约 10.7 GB，16 GB Mac 可用",
     "items": [
         {"role": "engine", "url": "https://github.com/leejet/stable-diffusion.cpp/releases/download/"
                                   "master-900-c92d73c/sd-master-c92d73c-bin-Darwin-macOS-26.6.2-arm64.zip"},
         {"role": "vae", "url": f"{HF}/Comfy-Org/Qwen-Image-2.1/resolve/main/vae/qwen_image_2.1_vae_bf16.safetensors"},
         {"role": "dit", "url": f"{HF}/leejet/Qwen-Image-2.1-GGUF/resolve/main/qwen_image_2.1-Q4_K.gguf"},
         {"role": "te", "url": f"{HF}/Qwen/Qwen3-VL-8B-Instruct-GGUF/resolve/main/Qwen3VL-8B-Instruct-Q4_K_M.gguf"},
         {"role": "vision", "url": f"{HF}/Qwen/Qwen3-VL-8B-Instruct-GGUF/resolve/main/mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf"},
     ]},
    {"name": "生图模型 Q8_0（高质量）", "desc": "7.7 GB，细节和手部更好，多占约 3 GB 内存",
     "items": [{"role": "dit", "url": f"{HF}/leejet/Qwen-Image-2.1-GGUF/resolve/main/qwen_image_2.1-Q8_0.gguf"}]},
    {"name": "生图模型 Q6_K", "desc": "6.0 GB，介于 Q4 和 Q8 之间",
     "items": [{"role": "dit", "url": f"{HF}/leejet/Qwen-Image-2.1-GGUF/resolve/main/qwen_image_2.1-Q6_K.gguf"}]},
    {"name": "Heretic 文本编码器", "desc": "去除拒答倾向的 Qwen3-VL，含配套视觉组件，约 6.2 GB",
     "items": [
         {"role": "te", "url": f"{HF}/pottokao/Qwen-Image-2.1-Text-Encoder-Heretic-GGUF/resolve/main/qwen3vl_8b_heretic-Q4_K_M.gguf"},
         {"role": "vision", "url": f"{HF}/pottokao/Qwen-Image-2.1-Text-Encoder-Heretic-GGUF/resolve/main/mmproj-qwen3vl_8b_heretic-f16.gguf"},
     ]},
    {"name": "MLX 引擎套装（实验）", "desc": "MLX-Serve 程序 72 MB + ddalcu 4bit 模型包 10.7 GB，苹果 MLX 框架",
     "items": [
         {"role": "mlx_engine", "url": "https://github.com/ddalcu/mlx-serve/releases/download/v26.9.5/"
                                       "mlx-serve-bin-macos-arm64.tar.gz"},
         {"role": "mlx", "url": f"{HF}/ddalcu/Qwen-Image-2.1-MLX-Serve-4bit"},
     ]},
]

lock = threading.Lock()
job = {"running": False, "stage": "空闲", "step": 0, "total": 0, "spi": None, "log": [],
       "error": None, "output": None, "proc": None, "started": None, "cancelled": False}


# ---------- 通用 ----------

def load_json(p, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def save_json(p, data):
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    tmp.replace(p)


def to_trash(path):
    """移到废纸篓（可恢复），不直接删除。"""
    r = subprocess.run(["/usr/bin/trash", str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "移到废纸篓失败")


def inside(base, rel):
    """把前端传来的相对路径解析到 base 内，越界返回 None。"""
    p = (base / rel).resolve()
    try:
        p.relative_to(base.resolve())
    except ValueError:
        return None
    return p


def system_proxy():
    try:
        out = subprocess.run(["scutil", "--proxy"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    kv = dict(re.findall(r"^\s*(\w+)\s*:\s*(\S+)\s*$", out, re.M))
    if kv.get("HTTPSEnable") == "1" and kv.get("HTTPSProxy"):
        return f"http://{kv['HTTPSProxy']}:{kv.get('HTTPSPort', '80')}"
    return None


def avail_mem_gb():
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
        page = int(re.search(r"page size of (\d+)", out).group(1))
    except Exception:
        return None
    vals = {k.strip(): int(v) for k, v in re.findall(r"^(.+?):\s+(\d+)\.$", out, re.M)}
    pages = sum(vals.get(k, 0) for k in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"))
    return round(pages * page / 2**30, 1)


def sd_server():
    for p in BIN.rglob("sd-server"):
        if p.is_file() and os.access(p, os.X_OK):
            return p
    return None


def mlx_serve():
    for p in (BIN / "mlx-serve").rglob("mlx-serve"):
        if p.is_file() and os.access(p, os.X_OK):
            return p
    return None


# ---------- 模型清单与选择 ----------

def infer_role(rel):
    p = MODELS / rel
    # 多文件仓库（diffusers / MLX 包）里的分片不能单独用
    for parent in p.parents:
        if parent == MODELS:
            break
        if (parent / "model_index.json").exists() or (parent / "config.json").exists():
            return "other"
    n = p.name.lower()
    if "mmproj" in n:
        return "vision"
    if "vae" in n:
        return "vae"
    if any(k in n for k in ("qwen3vl", "qwen3-vl", "qwen2.5-vl", "text_encoder", "t5xxl", "clip_")):
        return "te"
    return "dit"


def repo_root(p):
    """文件属于多文件仓库（diffusers / MLX 包）时，返回仓库最外层目录。"""
    root = None
    for parent in p.parents:
        if parent == MODELS:
            break
        if (parent / "model_index.json").exists() or (parent / "config.json").exists():
            root = parent
    return root


def inventory():
    st = load_json(SETTINGS, {})
    roles = st.get("roles", {})
    items, repos = [], {}
    for p in sorted(MODELS.rglob("*")):
        if not p.is_file() or p.name.endswith((".part", ".tmp")):
            continue
        repo = repo_root(p)
        if repo:  # 仓库整体算一项；根目录 config.json 标明 qwen_image 的是 MLX-Serve 包
            if repo not in repos:
                mt = str(load_json(repo / "config.json", {}).get("model_type", ""))
                repos[repo] = {"file": str(repo.relative_to(MODELS)), "size": 0, "dir": True,
                               "role": "mlx" if mt.startswith("qwen_image") else "other"}
            r = repos[repo]
            r["size"] += p.stat().st_size
        elif p.suffix.lower() in MODEL_EXT:
            rel = str(p.relative_to(MODELS))
            items.append({"file": rel, "size": p.stat().st_size, "role": roles.get(rel) or infer_role(rel)})
    return items + list(repos.values())


def selection(inv=None):
    inv = inventory() if inv is None else inv
    saved = load_json(SETTINGS, {}).get("sel", {})
    sel = {}
    for role in ("dit", "te", "vae", "vision", "mlx"):
        files = [i["file"] for i in inv if i["role"] == role]
        sel[role] = saved.get(role) if saved.get(role) in files else (files[0] if files else None)
    sel["engine"] = saved.get("engine") if saved.get("engine") in ("sd", "mlx") else "sd"
    return sel


def update_settings(fn):
    with lock:
        st = load_json(SETTINGS, {})
        fn(st)
        save_json(SETTINGS, st)


def load_history():
    return load_json(HISTORY, [])


def save_history(h):
    save_json(HISTORY, h)


# ---------- 下载队列 ----------

dl_lock = threading.Lock()
dl_state = {"worker": None, "proc": None, "current": None}


def tasks():
    return load_json(DOWNLOADS, [])


def save_tasks(ts):
    save_json(DOWNLOADS, ts)


def update_task(tid, **kw):
    with dl_lock:
        ts = tasks()
        for t in ts:
            if t["id"] == tid:
                t.update(kw)
        save_tasks(ts)


def curl_base(proxy):
    return ["curl", "-L", "--fail", "-sS", "--connect-timeout", "20"] + (["-x", proxy] if proxy else ["--noproxy", "*"])


def fetch_url(t, url):
    """按任务的下载源改写链接，并决定是否走代理。"""
    proxy = system_proxy()
    if "huggingface.co" in url and t.get("source") == "mirror":
        return url.replace("https://huggingface.co", "https://hf-mirror.com"), None
    return url, proxy


def remote_size(url, proxy):
    try:
        out = subprocess.run(curl_base(proxy) + ["-I", url], capture_output=True, text=True, timeout=40).stdout
    except Exception:
        return None
    sizes = re.findall(r"^(?:x-linked-size|content-length):\s*(\d+)", out, re.I | re.M)
    # 重定向链里最后一个是真实文件；x-linked-size 是 HF 给的真实大小
    linked = re.findall(r"^x-linked-size:\s*(\d+)", out, re.I | re.M)
    val = int(linked[-1]) if linked else (int(sizes[-1]) if sizes else 0)
    return val if val > 1024 else None


def dest_of(t):
    return ROOT / t["dest"]


def part_of(t):
    d = dest_of(t)
    return d.with_name(d.name + ".part")


def postprocess(t):
    """sd.cpp 程序压缩包：解压到 bin/ 并去掉隔离属性。"""
    d = dest_of(t)
    if t["role"] == "mlx_engine":
        dest = BIN / "mlx-serve"
        dest.mkdir(exist_ok=True)
        subprocess.run(["tar", "-xzf", str(d), "-C", str(dest)], check=True)
        for f in dest.rglob("*"):
            if f.is_file() and (f.name == "mlx-serve" or f.suffix in (".dylib", ".metallib") or os.access(f, os.X_OK)):
                f.chmod(0o755)
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(dest)], capture_output=True)
        to_trash(d)
        return
    if t["role"] != "engine":
        return
    if d.suffix == ".zip":
        subprocess.run(["unzip", "-o", "-q", str(d), "-d", str(BIN)], check=True)
    elif d.name.endswith((".tar.gz", ".tgz")):
        subprocess.run(["tar", "-xzf", str(d), "-C", str(BIN)], check=True)
    else:
        d.chmod(0o755)
        return
    for f in BIN.rglob("*"):
        if f.is_file() and (f.name.startswith("sd-") or f.suffix == ".dylib"):
            f.chmod(0o755)
    subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(BIN)], capture_output=True)
    to_trash(d)


def run_task(t):
    url, proxy = fetch_url(t, t["url"])
    size = t.get("size") or remote_size(url, proxy)
    if size:
        update_task(t["id"], size=size)
    final, part = dest_of(t), part_of(t)
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.exists() and (not size or final.stat().st_size == size):
        update_task(t["id"], status="done", error=None)
        return
    cmd = curl_base(proxy) + ["-C", "-", "--retry", "20", "--retry-delay", "3", "--retry-all-errors",
                              "-o", str(part), url]
    while True:
        cur = next((x for x in tasks() if x["id"] == t["id"]), None)
        if not cur or cur["status"] != "downloading":  # 重试等待期间被暂停或移除
            return
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        dl_state["proc"] = p
        _, err = p.communicate()
        dl_state["proc"] = None
        cur = next((x for x in tasks() if x["id"] == t["id"]), None)
        if not cur or cur["status"] != "downloading":  # 被暂停或移除
            return
        have = part.stat().st_size if part.exists() else 0
        if (size and have == size) or (not size and p.returncode == 0 and have > 0):
            part.rename(final)
            postprocess(t)
            update_task(t["id"], status="done", error=None)
            return
        if (size and have > size) or p.returncode == 33:  # 文件异常，或服务器不支持续传：从头下
            part.unlink(missing_ok=True)
        fatal = re.search(r"error: (401|403|404)", err or "") if p.returncode == 22 else None
        if fatal:
            code = fatal[1]
            msg = {"404": "链接不存在（404）"}.get(code, f"没有访问权限（{code}），可能需要先在网页上同意模型协议")
            update_task(t["id"], status="error", error=msg)
            return
        update_task(t["id"], error=(err or "").strip()[-200:] or f"curl 退出码 {p.returncode}，重试中…")
        time.sleep(5)


def download_worker():
    while True:
        with dl_lock:
            ts = tasks()
            t = next((x for x in ts if x["status"] == "queued"), None)
            if not t or dl_state.get("quitting"):
                dl_state["worker"] = None
                dl_state["current"] = None
                return
            t["status"] = "downloading"
            save_tasks(ts)
        dl_state["current"] = t["id"]
        try:
            run_task(t)
        except Exception as e:
            update_task(t["id"], status="error", error=str(e))


def kick_worker():
    with dl_lock:
        if dl_state["worker"] and dl_state["worker"].is_alive():
            return
        dl_state["worker"] = threading.Thread(target=download_worker, daemon=True)
        dl_state["worker"].start()


def expand_hf_repo(url, source):
    """整个 HF 仓库链接 → 仓库内每个文件一条下载任务。"""
    m = re.match(r"https://(?:huggingface\.co|hf-mirror\.com)/([^/]+)/([^/?#]+)(?:/tree/([^/?#]+)(/[^?#]*)?)?/?$", url)
    if not m:
        return None
    org, repo, rev, sub = m[1], m[2], m[3] or "main", (m[4] or "").strip("/")
    api = f"https://huggingface.co/api/models/{org}/{repo}/tree/{rev}" + (f"/{sub}" if sub else "") + "?recursive=true"
    api, proxy = fetch_url({"source": source}, api)
    r = subprocess.run(curl_base(proxy) + [api], capture_output=True, text=True, timeout=60)
    try:
        files = [f for f in json.loads(r.stdout) if f.get("type") == "file"]
    except Exception:
        raise RuntimeError("读取仓库文件列表失败：" + (r.stderr.strip() or r.stdout[:120]))
    return [(f"https://huggingface.co/{org}/{repo}/resolve/{rev}/{f['path']}",
             f"models/{org}/{repo}/{f['path']}", f.get("size")) for f in files]


def add_download(url, role, name, source):
    url = url.strip()
    if not re.match(r"https?://", url):
        raise ValueError("请填写以 http(s):// 开头的链接")
    if role not in ROLES:
        raise ValueError("未知的用途")
    url = url.replace("hf-mirror.com", "huggingface.co").replace("/blob/", "/resolve/")
    entries = expand_hf_repo(url, source) if "/resolve/" not in url else None
    if entries is None:
        fname = Path(name.strip()).name if name.strip() else unquote(Path(urlparse(url).path).name)
        if not fname:
            raise ValueError("没法从链接里看出文件名，请手动填写")
        dest = f"bin/{fname}" if role in ("engine", "mlx_engine") else f"models/{fname}"
        entries = [(url, dest, None)]
    for _, dest, _ in entries:
        if not inside(ROOT / dest.split("/")[0], dest.split("/", 1)[1]):
            raise ValueError("文件路径不合法：" + dest)
    added = 0
    with dl_lock:
        # 模型文件已被删掉的「已完成」任务不算数，否则预设没法重新下载
        ts = [t for t in tasks() if not (t["status"] == "done" and t["dest"].startswith("models/")
                                         and not dest_of(t).exists())]
        known = {t["dest"] for t in ts if t["status"] != "error"}
        for u, dest, size in entries:
            if dest in known:
                continue
            ts.append({"id": uuid.uuid4().hex[:10], "url": u, "role": role, "dest": dest, "size": size,
                       "name": Path(dest).name, "status": "queued", "error": None, "source": source})
            added += 1
        save_tasks(ts)
    if role in ("dit", "te", "vae", "vision"):
        update_settings(lambda st: [st.setdefault("roles", {}).__setitem__(str(Path(d).relative_to("models")), role)
                                    for _, d, _ in entries if d.startswith("models/")])
    kick_worker()
    return added


def task_action(tid, action):
    p = dl_state.get("proc")
    if action == "pause":
        update_task(tid, status="paused")
        if dl_state["current"] == tid and p and p.poll() is None:
            p.terminate()
    elif action == "resume":
        update_task(tid, status="queued", error=None)
        kick_worker()
    elif action == "remove":
        t = next((x for x in tasks() if x["id"] == tid), None)
        update_task(tid, status="removed")
        if dl_state["current"] == tid and p and p.poll() is None:
            p.terminate()
            time.sleep(0.3)
        if t and part_of(t).exists():
            to_trash(part_of(t))
        with dl_lock:
            save_tasks([x for x in tasks() if x["id"] != tid])
    elif action == "clear":
        with dl_lock:
            save_tasks([x for x in tasks() if x["status"] != "done"])


def stop_for_quit():
    """退出时停掉 curl，但任务保持排队，下次打开自动续传。"""
    dl_state["quitting"] = True
    with dl_lock:
        ts = tasks()
        for t in ts:
            if t["status"] == "downloading":
                t["status"] = "queued"
        save_tasks(ts)
    p = dl_state.get("proc")
    if p and p.poll() is None:
        p.terminate()


def task_view():
    out = []
    for t in tasks():
        part, final = part_of(t), dest_of(t)
        have = final.stat().st_size if t["status"] == "done" and final.exists() else (
            part.stat().st_size if part.exists() else 0)
        out.append({k: t.get(k) for k in ("id", "name", "role", "size", "status", "error", "url")} | {"have": have})
    return out


# ---------- 常驻引擎 ----------
# 引擎以服务方式常驻，模型加载一次后留在内存里，之后每张图都省掉加载时间。
# 同一时间只留一个引擎（sd-server 或 mlx-serve）；换模型、换引擎、切省内存模式时重启。

PROG = re.compile(r"\|\s*(\d+)/(\d+)\s*-\s*([\d.]+)(s/it|it/s)")
ENGINE_PID = TMP / "engine.pid"
eng_lock = threading.Lock()  # 同一时间只有一个线程在启动 / 使用引擎
# want：用户想让模型常驻（点了「加载模型」或出过图），点「释放内存」后为 False
eng = {"proc": None, "key": None, "port": None, "warming": False, "timer": None, "want": False}
# 走本机回环，不能被系统代理截走
local = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def free_port():
    # 随机端口：sd-server 对任何网页都放开了跨域，不用固定端口让别的网站难以撞上
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def eng_alive():
    return eng["proc"] is not None and eng["proc"].poll() is None


def eng_call(path, body=None, timeout=10):
    data = None if body is None else json.dumps(body).encode()
    r = urllib.request.Request(f"http://127.0.0.1:{eng['port']}{path}", data=data,
                               headers={"Content-Type": "application/json"})
    return local.open(r, timeout=timeout)


def http_detail(e):
    detail = e.read().decode("utf-8", "replace")[:500] if hasattr(e, "read") else ""
    return (str(e) + ("\n" + detail if detail else "")).strip()


def log_line(line):
    job["log"].append(line[:400])
    del job["log"][:-300]


def sd_line(line):
    live = job["running"] and not eng["warming"]  # 预热那张图的进度不算进任务
    m = PROG.search(line)
    if m:
        if not live:
            return
        n, t, v, unit = int(m[1]), int(m[2]), float(m[3]), m[4]
        # EasyCache 跳过的步报告的耗时接近 0，用实际经过时间求平均更准
        now = time.time()
        if n <= 1 or "t_first" not in job or n < job.get("step", 0):
            job["t_first"], job["n_first"] = now, n
            spi = v if unit == "s/it" else (1 / v if v else None)
        else:
            spi = (now - job["t_first"]) / max(1, n - job["n_first"]) if n > job["n_first"] else job["spi"]
        job.update(step=n, total=t, spi=spi)
        job["stage"] = "采样中" if n < t else "解码中（VAE）"
        return
    low = line.lower()
    if live and job["step"] == 0:
        if "load" in low:
            job["stage"] = "加载模型"
        elif "condition" in low or "encod" in low:
            job["stage"] = "编码提示词"
    log_line(line)


def pump_sd(stream):
    buf = b""
    while True:
        chunk = stream.read1(4096)
        if not chunk:
            break
        buf += chunk
        parts = re.split(rb"[\r\n]", buf)
        buf = parts.pop()
        # 进度行以 \r 结尾前就可能停住，尾巴里若已是完整进度也一并处理，避免显示慢一步
        tail = buf.decode("utf-8", "replace")
        if PROG.search(tail) and ("s/it" in tail or "it/s" in tail):
            parts.append(buf)
            buf = b""
        for raw in parts:
            line = raw.decode("utf-8", "replace").strip()
            if line:
                sd_line(line)


def pump_mlx(stream):
    for raw in iter(stream.readline, b""):
        line = raw.decode("utf-8", "replace").strip()
        if line:
            log_line("[mlx] " + line)


def stop_engine():
    p = eng["proc"]
    if p and p.poll() is None:
        try:
            os.killpg(p.pid, signal.SIGTERM)
            p.wait(timeout=10)
        except Exception:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except Exception:
                pass
    eng.update(proc=None, key=None, port=None)
    ENGINE_PID.unlink(missing_ok=True)


def kill_stale_engine():
    """上次异常退出留下的引擎还占着内存，启动时清掉。"""
    try:
        pid = int(ENGINE_PID.read_text())
        cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
        if "sd-server" in cmd or "mlx-serve" in cmd:
            os.killpg(pid, signal.SIGTERM)
    except Exception:
        pass
    ENGINE_PID.unlink(missing_ok=True)


def start_engine(key, argv, health, pump):
    stop_engine()
    port = free_port()
    p = subprocess.Popen(argv(port), cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         start_new_session=True)
    ENGINE_PID.write_text(str(p.pid))
    eng.update(proc=p, key=key, port=port)
    threading.Thread(target=pump, args=(p.stdout,), daemon=True).start()
    for _ in range(240):
        if p.poll() is not None:
            break
        try:
            eng_call(health, timeout=1).read()
            return
        except Exception:
            time.sleep(0.5)
    stop_engine()
    raise RuntimeError("引擎没能启动，看运行日志")


def sd_key(sel, lowmem):
    return ("sd", sel["dit"], sel["te"], sel["vae"], sel["vision"], bool(lowmem))


def ensure_sd(sel, lowmem, warm=False):
    key = sd_key(sel, lowmem)
    if eng_alive() and eng["key"] == key:
        return

    def argv(port):
        a = [str(sd_server()), "--listen-ip", "127.0.0.1", "--listen-port", str(port),
             "--diffusion-model", str(MODELS / sel["dit"]),
             "--vae", str(MODELS / sel["vae"]),
             "--llm", str(MODELS / sel["te"]),
             "--sampling-method", "euler", "--diffusion-fa", "-v",
             # Qwen 2.1 的 VAE 是 3D 卷积，M2 上 Metal 实现很慢，放 CPU 快 3 倍且结果一致
             "--backend", "vae=cpu"]
        if sel["vision"]:  # 权重按需加载，不放参考图时不占内存
            a += ["--llm_vision", str(MODELS / sel["vision"])]
        if lowmem:
            a += ["--params-backend", "te=disk"]
        return a
    start_engine(key, argv, "/sdcpp/v1/capabilities", pump_sd)
    if warm:  # 权重是第一次用到时才加载的，出一张极小的图把它们提前拉进内存
        eng["warming"] = True
        try:
            run_sd_job({"prompt": "warm up", "width": 256, "height": 256, "seed": 1,
                        "sample_params": {"sample_steps": 1}})
        finally:
            eng["warming"] = False


def run_sd_job(body):
    """向 sd-server 提交一张图并等它出完，返回 PNG 的 base64。"""
    p = eng["proc"]
    try:
        jid = json.loads(eng_call("/sdcpp/v1/img_gen", body, timeout=60).read())["id"]
    except Exception as e:
        if p.poll() is not None:
            raise RuntimeError("引擎意外退出，看运行日志")
        raise RuntimeError(http_detail(e))
    while True:
        time.sleep(0.5)
        if p.poll() is not None:
            raise RuntimeError("引擎意外退出，看运行日志")
        try:
            s = json.loads(eng_call(f"/sdcpp/v1/jobs/{jid}").read())
        except Exception:
            continue  # 偶尔超时不要紧，进程还活着就接着等
        if s["status"] == "completed":
            return s["result"]["images"][0]["b64_json"]
        if s["status"] in ("failed", "cancelled"):
            raise RuntimeError((s.get("error") or {}).get("message") or "生成失败")


def ensure_mlx(pack):
    key = ("mlx", pack)
    if eng_alive() and eng["key"] == key:
        return
    empty = TMP / "mlx-empty"
    empty.mkdir(exist_ok=True)
    # 关掉 16 GB 机器上拦加载的两道检查：常驻内存上限、加载前空闲内存预检
    start_engine(key, lambda port: [str(mlx_serve()), "--serve", "--host", "127.0.0.1", "--port", str(port),
                                    "--model-dir", str(empty), "--max-resident-mem", "0", "--skip-mem-preflight"],
                 "/health", pump_mlx)
    try:
        eng_call("/v1/load-model", {"model": str(MODELS / pack)}, timeout=900).read()
    except Exception as e:
        stop_engine()
        raise RuntimeError(http_detail(e))


def lock_engine():
    """等引擎空出来（可能正在预热），等的时候也能取消。"""
    while not eng_lock.acquire(timeout=0.5):
        if job["cancelled"]:
            raise RuntimeError("cancelled")


def preload():
    """把当前选中的模型提前加载进引擎；条件不满足时关掉引擎释放内存。"""
    if dl_state.get("quitting") or not eng["want"]:
        return
    with eng_lock:
        try:
            sel = selection()
            if sel["engine"] == "mlx":
                if mlx_serve() and sel["mlx"]:
                    ensure_mlx(sel["mlx"])
                else:
                    stop_engine()
            elif sd_server() and sel["dit"] and sel["te"] and sel["vae"]:
                ensure_sd(sel, load_json(SETTINGS, {}).get("lowmem", True), warm=True)
            else:
                stop_engine()
        except Exception as e:
            if eng["want"] and not job["running"]:
                log_line(f"加载模型失败：{e}")


def schedule_preload(delay=1.5):
    """连着切几次模型时只在最后一次之后预载。"""
    t = eng.get("timer")
    if t:
        t.cancel()
    eng["timer"] = threading.Timer(delay, preload)
    eng["timer"].daemon = True
    eng["timer"].start()


def load_engine(lowmem):
    if load_json(SETTINGS, {}).get("lowmem", True) != lowmem:
        update_settings(lambda st: st.__setitem__("lowmem", lowmem))
    eng["want"] = True
    schedule_preload(0)


def unload_engine():
    if job["running"]:
        raise ValueError("正在生成，先取消再释放")
    eng["want"] = False
    t = eng.get("timer")
    if t:
        t.cancel()
    p = eng["proc"]
    if p and p.poll() is None:  # 先杀进程，正在进行的预热会马上失败退出、让出锁
        os.killpg(p.pid, signal.SIGTERM)

    def clean():
        with eng_lock:
            if not eng["want"]:
                stop_engine()
    threading.Thread(target=clean, daemon=True).start()


def warm_state():
    """off 未加载 / loading 加载中 / ready 已就绪（给界面上的按钮用）"""
    if eng["want"] and not job["running"] and (eng["warming"] or eng_lock.locked()):
        return "loading"
    return "ready" if eng_alive() else "off"


def finish(record, b64=None):
    if b64:
        Path(record["file"]).write_bytes(base64.b64decode(b64))
    job["stage"] = "完成"
    job["output"] = record["name"]
    record["seconds"] = round(time.time() - job["started"])
    with lock:
        h = load_history()
        h.append(record)
        save_history(h)


def fail(e):
    if job["cancelled"]:
        job["stage"] = "已取消"
        schedule_preload()  # 引擎被关掉了，用户没点过释放就重新加载回来
    else:
        job["stage"] = "失败"
        job["error"] = str(e)


def sd_worker(sel, lowmem, body, record):
    eng["want"] = True
    try:
        lock_engine()
        try:
            if not (eng_alive() and eng["key"] == sd_key(sel, lowmem)):
                job["stage"] = "启动引擎"
            ensure_sd(sel, lowmem)
            job["proc"] = eng["proc"]
            if job["cancelled"]:
                raise RuntimeError("cancelled")
            job["stage"] = "编码提示词"
            b64 = run_sd_job(body)
        finally:
            eng_lock.release()
        finish(record, b64)
    except Exception as e:
        fail(e)
        if job["error"]:
            job["error"] += "\n" + "\n".join(job["log"][-12:])
    finally:
        job["running"] = False
        job["proc"] = None


def mlx_worker(record, pack):
    """用常驻的 MLX-Serve 流式生成一张图。"""
    eng["want"] = True
    try:
        lock_engine()
        try:
            if not (eng_alive() and eng["key"] == ("mlx", pack)):
                job["stage"] = "加载模型"
            ensure_mlx(pack)
            job["proc"] = eng["proc"]
            if job["cancelled"]:
                raise RuntimeError("cancelled")
            body = {"model": Path(pack).name, "prompt": record["prompt"], "size": record["size"],
                    "steps": record["steps"], "seed": record["seed"], "guidance_scale": record["cfg"], "stream": True}
            if record["negative"]:
                body["negative_prompt"] = record["negative"]
            job["stage"] = "编码提示词"
            resp = eng_call("/v1/images/generations", body, timeout=7200)
            b64 = None
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    ev = json.loads(data)
                except Exception:
                    continue
                if isinstance(ev.get("data"), list) and ev["data"] and ev["data"][0].get("b64_json"):
                    b64 = ev["data"][0]["b64_json"]
                elif ev.get("b64_json") or ev.get("image"):
                    b64 = ev.get("b64_json") or ev.get("image")
                step = ev.get("step") if ev.get("step") is not None else ev.get("current")
                total = ev.get("total") or ev.get("total_steps") or ev.get("steps")
                if step is not None and total:
                    now = time.time()
                    if "t_first" not in job:
                        job["t_first"], job["n_first"] = now, int(step)
                    n = int(step)
                    spi = (now - job["t_first"]) / (n - job["n_first"]) if n > job["n_first"] else job["spi"]
                    job.update(step=n, total=int(total), spi=spi,
                               stage="采样中" if n < int(total) else "解码中（VAE）")
                if ev.get("error"):
                    raise RuntimeError(str(ev["error"]))
        finally:
            eng_lock.release()
        if not b64:
            raise RuntimeError("MLX-Serve 没有返回图片，看运行日志")
        finish(record, b64)
    except Exception as e:
        fail(http_detail(e))
    finally:
        job["running"] = False
        job["proc"] = None


# ---------- 生成 ----------

def parse_params(req):
    """校验并解析生成参数；出错抛 ValueError，此时任务还没占用。"""
    prompt = (req.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("提示词不能为空")
    try:
        w, h = (int(x) for x in str(req.get("size") or "512x512").split("x"))
        steps = max(1, min(80, int(req.get("steps") or 20)))
        cfg = float(req.get("cfg") or 1.0)
        seed = int(req.get("seed") if req.get("seed") is not None else -1)
    except (TypeError, ValueError):
        raise ValueError("参数格式不对，检查一下尺寸 / 步数 / CFG / 种子")
    if seed < 0:
        seed = int.from_bytes(os.urandom(4), "big") & 0x7FFFFFFF
    return {"prompt": prompt, "negative": (req.get("negative") or "").strip(), "size": f"{w}x{h}",
            "w": w, "h": h, "steps": steps, "cfg": cfg, "seed": seed}


def decode_ref(ref):
    """参考图 data URL → (扩展名, 字节)。只接受 base64 编码的位图。"""
    m = re.match(r"data:image/([\w.+-]+);base64,(.*)", ref, re.S)
    if not m or m[1] == "svg+xml":
        raise ValueError("参考图需要是 PNG / JPG 等图片文件")
    try:
        data = base64.b64decode(m[2], validate=True)
    except ValueError:
        raise ValueError("参考图读取失败，换一张试试")
    return {"jpeg": "jpg"}.get(m[1], m[1]), data


def claim_job():
    with lock:
        if job["running"]:
            return False
        job.update(running=True, stage="启动中", step=0, total=0, spi=None, log=[], error=None,
                   output=None, started=time.time(), cancelled=False)
        job.pop("t_first", None)
        return True


def start_generate(req):
    exe = sd_server()
    sel = selection()
    ref = req.get("ref")
    if sel["engine"] == "mlx":
        return start_generate_mlx(req, sel)
    need = ["dit", "te", "vae"] + (["vision"] if ref else [])
    missing = ([] if exe else ["sd.cpp 程序"]) + [ROLES[r] for r in need if not sel[r]]
    if missing:
        return "还缺：" + "、".join(missing) + "（点右上角「模型」下载）"
    try:
        prm = parse_params(req)
        ref_img = decode_ref(ref) if ref else None
    except ValueError as e:
        return str(e)
    if not claim_job():
        return "已有任务在跑"
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = OUT / f"qwen_{stamp}.png"
    lowmem = bool(req.get("lowmem", True))
    if load_json(SETTINGS, {}).get("lowmem", True) != lowmem:  # 记住，下次打开按这个预载
        update_settings(lambda st: st.__setitem__("lowmem", lowmem))
    fast = bool(req.get("fast", False))
    body = {"prompt": prm["prompt"], "negative_prompt": prm["negative"], "width": prm["w"], "height": prm["h"],
            "seed": prm["seed"], "output_format": "png",
            "sample_params": {"sample_method": "euler", "sample_steps": prm["steps"],
                              "guidance": {"txt_cfg": prm["cfg"]}},
            # EasyCache：相邻步变化小时跳过计算
            "cache_mode": "easycache" if fast else "disabled"}
    if ref_img:
        body["ref_images"] = [base64.b64encode(ref_img[1]).decode()]
        if req.get("reflite"):  # 参考图只按 1/4 像素进 DiT，序列短了每步快不少，代价是原图细节少些
            body["ref_image_args"] = f"vae_input_max_pixels={prm['w'] * prm['h'] // 4}"
    record = {"file": str(out), "name": out.name, "prompt": prm["prompt"], "negative": prm["negative"],
              "size": prm["size"], "steps": prm["steps"], "cfg": prm["cfg"], "seed": prm["seed"], "ref": bool(ref_img),
              "reflite": bool(ref_img and req.get("reflite")), "fast": fast, "time": stamp, "dit": Path(sel["dit"]).name, "te": Path(sel["te"]).name}
    threading.Thread(target=sd_worker, args=(sel, lowmem, body, record), daemon=True).start()
    return None


def start_generate_mlx(req, sel):
    missing = ([] if mlx_serve() else ["MLX-Serve 程序"]) + ([] if sel["mlx"] else ["MLX 模型包"])
    if missing:
        return "还缺：" + "、".join(missing) + "（点右上角「模型」下载「MLX 引擎套装」）"
    if req.get("ref"):
        return "MLX 引擎的 Qwen 2.1 暂不支持参考图编辑，请切回 sd.cpp 引擎"
    try:
        prm = parse_params(req)
    except ValueError as e:
        return str(e)
    if not claim_job():
        return "已有任务在跑"
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = OUT / f"qwen_{stamp}.png"
    record = {"file": str(out), "name": out.name, "prompt": prm["prompt"], "negative": prm["negative"],
              "size": prm["size"], "steps": prm["steps"], "cfg": prm["cfg"], "seed": prm["seed"], "ref": None,
              "fast": False, "time": stamp, "engine": "mlx", "dit": Path(sel["mlx"]).name}
    threading.Thread(target=mlx_worker, args=(record, sel["mlx"]), daemon=True).start()
    return None


def cancel_generate():
    if not job["running"]:
        return
    job["cancelled"] = True  # 进程还没起来时，由工作线程启动后自己检查
    p = job.get("proc")
    if p and p.poll() is None:  # 引擎没法中途打断一张图，只能关掉，之后在后台重新预载
        os.killpg(p.pid, signal.SIGTERM)


def delete_output(name):
    f = inside(OUT, Path(name).name)
    if f and f.exists():
        to_trash(f)
    with lock:
        save_history([r for r in load_history() if r.get("name") != Path(name).name])


def delete_model(rel):
    f = inside(MODELS, rel)
    if not f or f == MODELS.resolve() or not f.exists():
        raise ValueError("文件不存在")
    to_trash(f)
    # 删掉仓库后，清理留下的空父目录（如 models/mlx/ddalcu）
    for parent in f.parents:
        if parent == MODELS.resolve() or any(parent.iterdir()):
            break
        parent.rmdir()

    def clean(st):
        st.get("roles", {}).pop(rel, None)
        for k, v in list(st.get("sel", {}).items()):
            if v == rel:
                st["sel"].pop(k)
    update_settings(clean)


def status():
    inv = inventory()
    eta = None
    if job["running"] and job["spi"] and job["total"]:
        eta = round((job["total"] - job["step"]) * job["spi"])
    return {
        "engine": bool(sd_server()),
        "mlx_engine": bool(mlx_serve()),
        "models": inv,
        "sel": selection(inv),
        "tasks": task_view(),
        "job": {k: job[k] for k in ("running", "stage", "step", "total", "spi", "error", "output")}
               | {"eta": eta, "elapsed": round(time.time() - job["started"]) if job["started"] else 0,
                  "log": job["log"][-80:]},
        "mem": avail_mem_gb(),
        "disk": round(shutil.disk_usage(ROOT).free / 2**30, 1),
        "warm": warm_state(),
    }


# ---------- HTTP ----------

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def trusted(self):
        """挡住别的网站借用户浏览器调接口（CSRF）和 DNS rebinding：只认本机地址。"""
        hosts = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
        origin = self.headers.get("Origin")
        return self.headers.get("Host") in hosts and (origin is None or origin in {f"http://{h}" for h in hosts})

    def do_GET(self):
        if not self.trusted():
            return self.send(403, {"error": "forbidden"})
        path = unquote(urlparse(self.path).path)
        if path == "/":
            return self.send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/status":
            return self.send(200, status())
        if path == "/api/meta":
            return self.send(200, {"roles": ROLES, "presets": PRESETS})
        if path == "/api/history":
            return self.send(200, [r for r in load_history() if (OUT / r["name"]).exists()][::-1])
        for prefix, base in (("/outputs/", OUT), ("/tmp/", TMP)):
            if path.startswith(prefix):
                f = inside(base, path[len(prefix):])
                if f and f.is_file():
                    ctype = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}.get(
                        f.suffix[1:].lower(), "application/octet-stream")
                    return self.send(200, f.read_bytes(), ctype)
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.trusted():
            return self.send(403, {"error": "forbidden"})
        path = urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            if path == "/api/generate":
                err = start_generate(req)
                if err:
                    return self.send(400, {"error": err})
            elif path == "/api/cancel":
                cancel_generate()
            elif path == "/api/engine/load":
                load_engine(bool(req.get("lowmem", True)))
            elif path == "/api/engine/unload":
                unload_engine()
            elif path == "/api/dl/add":
                items = req.get("items") or [{"url": req.get("url", ""), "role": req.get("role", "dit"),
                                              "name": req.get("name", "")}]
                added = sum(add_download(i["url"], i.get("role", "dit"), i.get("name", ""), req.get("source", "hf"))
                            for i in items)
                return self.send(200, {"ok": True, "added": added})
            elif path == "/api/dl/action":
                task_action(req.get("id"), req.get("action"))
            elif path == "/api/select":
                role, f = req.get("role"), req.get("file")
                if role in ("dit", "te", "vae", "vision", "mlx", "engine"):
                    update_settings(lambda st: st.setdefault("sel", {}).__setitem__(role, f))
                    schedule_preload()  # 模型已加载着的话换成新选的
            elif path == "/api/model/role":
                rel, role = req.get("file"), req.get("role")
                if role in ROLES and inside(MODELS, rel):
                    update_settings(lambda st: st.setdefault("roles", {}).__setitem__(rel, role))
            elif path == "/api/model/delete":
                delete_model(req.get("file", ""))
                schedule_preload()
            elif path == "/api/history/delete":
                delete_output(req.get("name", ""))
            elif path == "/api/reveal":
                f = inside(OUT, Path(req.get("name", "")).name)
                subprocess.run(["open", "-R", str(f)] if f and f.exists() else ["open", str(OUT)])
            elif path == "/api/quit":
                self.send(200, {"ok": True})
                stop_for_quit()
                cancel_generate()
                stop_engine()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            else:
                return self.send(404, {"error": "not found"})
        except Exception as e:
            return self.send(400, {"error": str(e)})
        self.send(200, {"ok": True})


def main():
    # 上次异常退出时停在“下载中”的任务改回排队，打开后自动续传
    ts = tasks()
    for t in ts:
        if t["status"] == "downloading":
            t["status"] = "queued"
    save_tasks(ts)
    url = f"http://127.0.0.1:{PORT}/"
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    except OSError:  # 已经在运行：原生窗口会自己连上，只有命令行启动时才打开浏览器
        if os.environ.get("QWEN_NO_BROWSER") != "1":
            webbrowser.open(url)
        return
    if any(t["status"] == "queued" for t in ts):
        kick_worker()
    kill_stale_engine()
    if os.environ.get("QWEN_NO_BROWSER") != "1":
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    def on_term(*_):
        stop_for_quit()
        cancel_generate()
        stop_engine()
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, on_term)
    print("Qwen 生图已启动：", url, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
