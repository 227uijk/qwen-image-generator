#!/usr/bin/env python3
"""引擎参数测速：用 app 里当前选中的模型，逐个试几种 sd-server 参数，比较速度、内存和画面。

用法（先在 app 里点「卸载」或直接退出 app，别让两个引擎抢内存）：
    python3 tools/bench.py              # 比较 VAE 放 CPU / GPU、直接卷积几种组合
    python3 tools/bench.py --cache      # 另外比较 EasyCache / Spectrum 加速（不带 LoRA、12 步）
跑完把最后打印的表格发回来；图片在 bench_out/ 里，可以对比画质。
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def pick_data_dir():
    # 和启动器同样的规则：仓库里有 models/ 用仓库，否则用独立安装的数据目录
    if (REPO / "models").is_dir():
        return REPO
    return Path.home() / "Library/Application Support/QwenImage"


ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--data-dir", type=Path, default=None, help="模型所在的数据目录（默认自动找）")
ap.add_argument("--size", default="512x512")
ap.add_argument("--runs", type=int, default=2, help="每种参数出几张（第一张含提示词编码，取最后一张的采样 / VAE 时间）")
ap.add_argument("--cooldown", type=int, default=30, help="两组之间歇几秒，免得前一组的发热拖慢后一组")
ap.add_argument("--cache", action="store_true", help="另外测 EasyCache / Spectrum")
ap.add_argument("--prompt", default="一只橘猫坐在窗台上，傍晚的阳光，胶片质感，窗外是城市街道")
args = ap.parse_args()

os.environ["QWEN_DATA_DIR"] = str(args.data_dir or pick_data_dir())
import app  # noqa: E402  按上面的数据目录取模型和程序

OUT = REPO / "bench_out"
OUT.mkdir(exist_ok=True)
W, H = (int(x) for x in args.size.split("x"))
SEED = 42


def base_argv(sel, port):
    """和 app.ensure_sd 一致的参数（不含文本编码器放磁盘的情况）。"""
    a = [str(app.sd_server()), "--listen-ip", "127.0.0.1", "--listen-port", str(port),
         "--diffusion-model", str(app.MODELS / sel["dit"]), "--vae", str(app.MODELS / sel["vae"]),
         "--llm", str(app.MODELS / sel["te"]), "--sampling-method", "euler", "--diffusion-fa", "-v",
         "--lora-model-dir", str(app.MODELS), "--mmap", "--backend", "vae=cpu", "--auto-fit", "off"]
    if sel["vision"]:
        a += ["--llm_vision", str(app.MODELS / sel["vision"])]
    return a


def without(argv, flag, nvals=1):
    i = argv.index(flag)
    return argv[:i] + argv[i + 1 + nvals:]


# 名字、说明、怎么改参数
ENGINES = [
    ("基线", "现在 app 用的参数", lambda a: a),
    ("CPU+直接卷积", "VAE 仍在 CPU，换成直接卷积（不展开 im2col，省内存）", lambda a: a + ["--vae-conv-direct"]),
    ("GPU", "VAE 放回 Metal", lambda a: without(a, "--backend")),
    ("GPU+直接卷积", "VAE 放 Metal、直接卷积；Metal 的 3D 卷积只认 F16，所以 VAE 权重转成 F16",
     lambda a: without(a, "--backend") + ["--vae-conv-direct", "--tensor-type-rules", r"^first_stage_model\.=f16"]),
]


def footprint(pid):
    """进程的物理内存占用（当前, 峰值），GB。mmap 进来、可随时丢掉的模型页不算在内，比 RSS 准。"""
    try:
        out = subprocess.run(["vmmap", "-summary", str(pid)], capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return None, None
    def gb(label):
        m = re.search(label + r":\s*([\d.]+)([KMG])", out)
        return m and round(float(m[1]) * {"K": 1 / 2**20, "M": 1 / 2**10, "G": 1}[m[2]], 2)
    return gb(r"Physical footprint"), gb(r"Physical footprint \(peak\)")


def throttle():
    """macOS 当前的 CPU 限速百分比（100 = 没降频）。"""
    try:
        out = subprocess.run(["pmset", "-g", "therm"], capture_output=True, text=True, timeout=10).stdout
        m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", out)
        return int(m[1]) if m else None
    except Exception:
        return None


class Engine:
    def __init__(self, argv_fn, sel):
        self.port = app.free_port()
        self.timing, self.log = {}, []
        self.p = subprocess.Popen(argv_fn(base_argv(sel, self.port)), stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, start_new_session=True)
        threading.Thread(target=self.pump, daemon=True).start()
        app.eng["port"] = self.port  # 借用 app.eng_call
        for _ in range(240):
            if self.p.poll() is not None:
                raise RuntimeError("引擎没能启动：" + " | ".join(self.log[-5:]))
            try:
                app.eng_call("/sdcpp/v1/capabilities", timeout=1).read()
                return
            except Exception:
                time.sleep(0.5)
        raise RuntimeError("引擎启动超时")

    def pump(self):
        for raw in iter(self.p.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            self.log.append(line[:300])
            del self.log[:-50]
            t = app.TIMING.search(line)
            if t:
                self.timing[app.TIMING_KEYS[t[1]]] = float(t[2])

    def gen(self, body):
        """出一张图，返回 (PNG 字节, 墙钟秒数, 分段耗时)。"""
        self.timing = {}
        t0 = time.time()
        jid = json.loads(app.eng_call("/sdcpp/v1/img_gen", body, timeout=60).read())["id"]
        while True:
            time.sleep(0.2)
            if self.p.poll() is not None:
                raise RuntimeError("引擎中途退出（多半是内存不够）：" + " | ".join(self.log[-5:]))
            s = json.loads(app.eng_call(f"/sdcpp/v1/jobs/{jid}").read())
            if s["status"] == "completed":
                wall = time.time() - t0
                for _ in range(20):
                    if "total" in self.timing:
                        break
                    time.sleep(0.05)
                return base64.b64decode(s["result"]["images"][0]["b64_json"]), wall, dict(self.timing)
            if s["status"] in ("failed", "cancelled"):
                raise RuntimeError((s.get("error") or {}).get("message") or "生成失败")

    def stop(self):
        app.kill_group(self.p)
        try:
            self.p.wait(timeout=15)
        except Exception:
            pass


def body_for(sel, lora=True, steps=12, cache="disabled"):
    nodes = app.turbo_nodes(sel["lora"]) if lora else None
    b = {"prompt": args.prompt, "negative_prompt": "", "width": W, "height": H, "seed": SEED, "output_format": "png",
         "sample_params": {"sample_method": "euler", "sample_steps": len(nodes) if nodes else steps,
                           "guidance": {"txt_cfg": 1.0}},
         "cache_mode": cache}
    if lora and sel["lora"]:
        b["lora"] = [{"path": sel["lora"], "multiplier": 1.0}]
    if nodes:
        b["sample_params"]["custom_sigmas"] = app.turbo_sigmas(nodes, W, H)
    return b


def run_engine(name, argv_fn, sel, bodies):
    """起一个引擎，按 bodies 依次出图；每组返回一行结果。"""
    rows = []
    mem0 = app.avail_mem_gb()
    try:
        e = Engine(argv_fn, sel)
    except Exception as ex:
        return [{"name": name, "error": str(ex)}]
    try:
        e.gen({"prompt": "warm up", "width": 256, "height": 256, "seed": 1, "sample_params": {"sample_steps": 1},
               **({"lora": bodies[0][1]["lora"]} if "lora" in bodies[0][1] else {})})
        for label, body in bodies:
            res = []
            for i in range(args.runs):
                png, wall, t = e.gen(body)
                res.append((wall, t))
                print(f"  {label} 第 {i + 1} 张：{wall:.1f}s  {t}", flush=True)
            (OUT / f"{label}.png").write_bytes(png)
            first, last = res[0], res[-1]
            rows.append({"name": label, "wall": last[0], "first": first[0], "cond": first[1].get("cond"),
                         "sample": last[1].get("sample"), "vae": last[1].get("vae")})
        cur, peak = footprint(e.p.pid)
        for r in rows:
            r.update(peak=peak, mem0=mem0, throttle=throttle())
    except Exception as ex:
        rows.append({"name": name, "error": str(ex)})
    finally:
        e.stop()
    return rows


def fmt(v, unit="s"):
    return "—" if v is None else f"{v:.1f}{unit}"


def main():
    if subprocess.run(["pgrep", "-x", "sd-server"], capture_output=True).returncode == 0:
        sys.exit("有 sd-server 在跑（app 里的模型还没卸载？），先在 app 里点「卸载」或退出 app 再测")
    sel = app.selection()
    if not (app.sd_server() and sel["dit"] and sel["te"] and sel["vae"]):
        sys.exit(f"缺模型或 sd.cpp 程序（数据目录 {app.ROOT}），先在 app 里下好")
    print(f"数据目录 {app.ROOT}\n模型 {sel['dit']} · 文本编码器 {sel['te']} · LoRA {sel['lora'] or '无'} · {W}x{H}\n")

    rows = []
    for i, (name, desc, fn) in enumerate(ENGINES):
        if i:
            time.sleep(args.cooldown)
        print(f"[{name}] {desc}", flush=True)
        rows += run_engine(name, fn, sel, [(name, body_for(sel))])
    if args.cache:
        time.sleep(args.cooldown)
        print("[加速缓存] 不带 LoRA、12 步，同一个引擎里比较", flush=True)
        rows += run_engine("缓存", ENGINES[0][2], {**sel, "lora": None},
                           [(f"缓存-{m}", body_for(sel, lora=False, cache=m)) for m in ("disabled", "easycache", "spectrum")])

    lines = ["| 参数 | 单张 | 首张 | 编码 | 采样 | VAE | 峰值内存 | 起跑前可用 | CPU 限速 |", "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if "error" in r:
            lines.append(f"| {r['name']} | 失败：{r['error'][:80]} |||||||| ")
            continue
        lines.append(f"| {r['name']} | {fmt(r['wall'])} | {fmt(r['first'])} | {fmt(r['cond'])} | {fmt(r['sample'])} | "
                     f"{fmt(r['vae'])} | {fmt(r['peak'], ' GB')} | {fmt(r['mem0'], ' GB')} | "
                     f"{'—' if r['throttle'] is None else str(r['throttle']) + '%'} |")
    table = "\n".join(lines)
    (OUT / "result.md").write_text(table + "\n")
    print("\n" + table + f"\n\n结果和图片在 {OUT}/")


if __name__ == "__main__":
    main()
