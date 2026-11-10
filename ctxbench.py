#!/usr/bin/env python3
"""ctxbench — 长上下文注意力阶梯测试，经由 opencode 调用任意供应商的模型。

  python ctxbench.py calibrate            # 量出每个模型的 token/字符 比例（新模型先跑这个）
  python ctxbench.py run                  # 按 config.json 的阶梯从大到小跑，可断点续跑
  python ctxbench.py run --models opencode-go/glm-5.3-flash --rungs 32000 --trials 1
  python ctxbench.py report               # 生成 results/report.md、results/chart.svg，并刷新 README 榜单

只依赖 Python 标准库和本机的 opencode（桌面版或 CLI 均可）。
"""
import argparse, base64, json, math, os, random, re, shutil, subprocess, sys, threading, time
import urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date as _date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
RESULTS = ROOT / "results"
RAW = RESULTS / "raw.jsonl"
CALIB = RESULTS / "calibration.json"
README = ROOT / "README.md"
LEGACY_PROVIDER = "opencode-go"  # 早期记录只存了模型名，没有供应商前缀
WEIGHTS = {"retrieve": 0.40, "update": 0.25, "count": 0.20, "sign": 0.15}
PASS_AVG, PASS_WORST = 0.90, 0.80   # “可用上限”门槛


def load_config():
    cfg = json.loads(CONFIG.read_text("utf-8"))
    for m in cfg["models"]:
        if "/" not in m["model"]:
            sys.exit(f"config.json: 模型需写成 provider/model 形式：{m['model']}")
        m.setdefault("variant", None)
        m.setdefault("name", m["model"].split("/", 1)[1])
    return cfg


def norm(model):
    return model if "/" in model else f"{LEGACY_PROVIDER}/{model}"

# ---------------------------------------------------------------- opencode 接入

def find_cli():
    cands = [os.environ.get("OPENCODE_CLI"), shutil.which("opencode"), shutil.which("opencode-cli")]
    if os.name == "nt" and os.environ.get("APPDATA"):  # Windows 桌面版自带的 CLI
        cands += [str(p) for p in sorted((Path(os.environ["APPDATA"]) / "ai.opencode.desktop" / "cli").glob("*/opencode-cli.exe"), reverse=True)]
    for c in cands:
        if c and Path(c).exists():
            return c
    sys.exit("找不到 opencode。请安装 opencode，或设置环境变量 OPENCODE_CLI 指向它的可执行文件。")


def server():
    """返回 (url, 认证头)。默认连接 opencode 的后台服务；也可用环境变量指定。"""
    url = os.environ.get("OPENCODE_SERVER_URL")
    if not url:
        out = subprocess.run([find_cli(), "api", "GET", "/api/info"], capture_output=True, text=True, encoding="utf-8")
        try:
            url = json.loads(out.stdout)["urls"][0]
        except Exception:
            sys.exit(f"无法连接 opencode 后台服务（可先运行 `opencode service start`）：{out.stdout[:300]} {out.stderr[:300]}")
    pw = os.environ.get("OPENCODE_SERVER_PASSWORD")
    svc = Path.home() / ".config" / "opencode" / "service.json"
    if pw is None and svc.exists():
        pw = json.loads(svc.read_text("utf-8")).get("password")
    auth = {"Authorization": "Basic " + base64.b64encode(f"opencode:{pw}".encode()).decode()} if pw else {}
    return url.rstrip("/"), auth


def generate(url, auth, model, variant, prompt, timeout):
    provider, mid = model.split("/", 1)
    ref = {"providerID": provider, "id": mid}
    if variant:
        ref["variant"] = variant
    req = urllib.request.Request(url + "/api/experimental/generate/text", method="POST",
                                 data=json.dumps({"prompt": prompt, "model": ref}).encode(),
                                 headers={**auth, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())["data"]["text"], None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:600]}"
    except Exception as e:  # 超时、连接中断等
        return None, f"{type(e).__name__}: {e}"

# ---------------------------------------------------------------- 归档生成

SURN = "王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵"
GIVEN = "伟芳娜敏静丽强磊军洋勇艳杰娟涛明超秀霞平刚桂英华玉兰飞鑫波斌宇浩凯健俊帆帅旭宁龙林欢阳博文辉力颖琳雪倩慧晨瑞梓轩睿昊然诗涵悦萱"
DEPTS = ["平台组", "数据组", "运维组", "财务部", "行政部", "采购部", "客服中心", "法务部", "市场部", "测试组", "前端组", "仓储部"]
ROOMS = ["A座3层机房", "B座档案室", "C座2层库房", "东区备件间", "西区实验室", "南楼服务台", "北楼7层弱电间", "一号仓冷藏区", "总部5层会议区", "二期厂房夹层"]
SITES = ["总部前台", "东区厂房", "西区仓库", "南楼大厅", "北楼门岗", "一号仓", "客户现场", "培训教室"]
SERVICES = ["order-sync", "billing-core", "inventory-api", "notify-hub", "report-gen", "auth-gateway", "search-index", "file-store", "price-engine", "audit-trail"]
TOPICS = ["季度预算调整", "供应商续约", "机房搬迁排期", "报销流程简化", "新员工入职培训", "客户投诉复盘", "库存盘点差异", "系统升级窗口", "数据备份策略", "办公用品采购", "差旅标准更新", "接口限流方案"]
DECISIONS = ["维持现有方案", "延期两周再议", "先做小范围试点", "交由下一级评审", "按修订版执行", "补充材料后重新提交", "暂不采纳", "本月内完成落地"]
ACTIONS = ["提交周报", "更新值班表", "确认发票信息", "回访客户", "核对出库单", "整理会议材料", "续签门禁卡", "归还借用设备", "完成安全培训", "检查备份日志", "同步需求文档", "预约会议室"]
CHATS = ["这个我下午看一下，晚点回复你。", "收到，已经转给对应的同事了。", "昨天那份表格好像少了一列，麻烦再发一次。", "会议改到三点了，地点不变。",
         "打印机又卡纸了，已经报修。", "链接打不开，是不是权限没开？", "我这边没问题，可以合并。", "周五之前给到初稿应该来得及。",
         "快递到了，放在前台了。", "这个需求先放一放，优先处理线上的。", "谢谢，已经解决了。", "明天我请假，有事找组长。"]
STATUS = ["成功", "超时后重试成功", "返回 502 已告警", "被限流", "降级处理", "完成但有 3 条脏数据"]
ALNUM = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def name(r):
    return r.choice(SURN) + r.choice(GIVEN) + (r.choice(GIVEN) if r.random() < 0.7 else "")


def date(r):
    return f"2026-{r.randint(1, 10):02d}-{r.randint(1, 28):02d}"


def code(r):
    return "".join(r.choice(ALNUM) for _ in range(8))


def asset_line(r, dev, c, who=None):
    return f"资产登记：设备 {dev} 存放于{r.choice(ROOMS)}，保管人{who or name(r)}，资产码 {c}。"


def fix_line(dev, c):
    return f"更正通知：设备 {dev} 的资产码已变更为 {c}，旧码作废。"


def checkin_line(r, who):
    return f"签到记录：{who} 于 {date(r)} 在{r.choice(SITES)}完成现场签到。"


def filler(r, new_dev, people):
    k = r.random()
    if k < 0.22:
        return (f"[{date(r)} {r.randint(0, 23):02d}:{r.randint(0, 59):02d}] 服务 {r.choice(SERVICES)} 处理批次 "
                f"B-{r.randint(10000, 99999)} {r.choice(STATUS)}，耗时 {r.randint(12, 9800)}ms。")
    if k < 0.40:
        return f"会议纪要：{r.choice(DEPTS)}讨论了{r.choice(TOPICS)}，结论是{r.choice(DECISIONS)}，负责人{r.choice(people)}，截止 {date(r)}。"
    if k < 0.55:
        return f"待办：{r.choice(people)}需要在 {date(r)} 前{r.choice(ACTIONS)}。"
    if k < 0.70:
        return f"{r.choice(people)}：{r.choice(CHATS)}"
    if k < 0.84:
        return asset_line(r, new_dev(), code(r), r.choice(people))
    if k < 0.94:
        return checkin_line(r, r.choice(people))
    return fix_line(new_dev(), code(r))


def build(target_tokens, tokens_per_char, seed):
    """返回 (prompt, answer_key)。同一 seed 下所有模型拿到的题目内容相同，只是长度按各自分词器换算。"""
    r = random.Random(seed)
    used = set()

    def new_dev():
        while True:
            d = f"DV-{r.randint(10000, 99999)}"
            if d not in used:
                used.add(d)
                return d

    people = sorted({name(r) for _ in range(60)})
    target_person = "欧阳" + r.choice(GIVEN) + r.choice(GIVEN)
    while target_person in people:
        target_person = "欧阳" + r.choice(GIVEN) + r.choice(GIVEN)
    sign = f"ZETA-{r.randint(1000, 9999)}"

    retrieve = [(new_dev(), code(r), min(0.98, max(0.02, 0.05 + 0.1 * i + r.uniform(-0.03, 0.03)))) for i in range(10)]
    update = []
    for _ in range(4):
        d0 = r.uniform(0.05, 0.45)
        update.append((new_dev(), code(r), code(r), d0, d0 + r.uniform(0.2, 0.5)))
    n_count = r.randint(5, 9)
    count_depths = sorted(r.uniform(0.02, 0.98) for _ in range(n_count))

    header = ("以下是一份工作记录归档，请完整阅读，归档结束后有问题需要回答。\n"
              f"答题规则：最终答案 JSON 中的 sign 字段必须填写 {sign}。\n"
              "===== 归档开始 =====\n")
    asked = [d for d, *_ in retrieve] + [d for d, *_ in update]
    r.shuffle(asked)
    question = ("\n===== 归档结束 =====\n"
                "问题：请只输出一个 JSON 对象，不要输出任何其它文字，格式为\n"
                '{"assets": {"DV-xxxxx": "资产码", ...}, "count": 整数, "sign": "..."}\n'
                f"1) assets：给出以下 {len(asked)} 台设备当前有效的资产码（若该设备有更正通知，以更正后的为准）：{'、'.join(asked)}。\n"
                f"2) count：{target_person} 的签到记录在归档中一共有多少条。\n"
                "3) sign：按归档开头“答题规则”的要求填写。\n")

    budget = int(target_tokens / tokens_per_char) - len(header) - len(question)
    lines, size = [], 0
    while size < budget:
        ln = filler(r, new_dev, people)
        lines.append(ln)
        size += len(ln) + 1

    inserts = [(d, asset_line(r, dev, c)) for dev, c, d in retrieve]
    for dev, c0, c1, d0, d1 in update:
        inserts += [(d0, asset_line(r, dev, c0)), (d1, fix_line(dev, c1))]
    inserts += [(d, checkin_line(r, target_person)) for d in count_depths]
    n = len(lines)
    for d, ln in sorted(inserts, key=lambda x: -x[0]):  # 从后往前插，前面的位置不受影响
        lines.insert(int(d * n), ln)

    key = {"retrieve": {dev: {"code": c, "depth": round(d, 3)} for dev, c, d in retrieve},
           "update": {dev: {"code": c1, "old": c0, "depth": [round(d0, 3), round(d1, 3)]} for dev, c0, c1, d0, d1 in update},
           "count": n_count, "sign": sign}
    return header + "\n".join(lines) + question, key

# ---------------------------------------------------------------- 评分

def parse_answer(text):
    for m in reversed(list(re.finditer(r"\{", text or ""))):  # 取最后一个能解析的最外层 JSON
        depth = 0
        for i in range(m.start(), len(text)):
            depth += text[i] == "{"
            depth -= text[i] == "}"
            if depth == 0:
                try:
                    obj = json.loads(text[m.start():i + 1])
                    if isinstance(obj, dict) and ("assets" in obj or "count" in obj):
                        return obj
                except json.JSONDecodeError:
                    pass
                break
    return None


def score(text, key):
    ans = parse_answer(text)
    if ans is None:
        return {"parsed": False, "retrieve": 0.0, "update": 0.0, "count": 0.0, "sign": 0.0, "composite": 0.0,
                "retrieve_miss_depths": [v["depth"] for v in key["retrieve"].values()], "update_stale": 0, "count_given": None}
    assets = ans.get("assets") if isinstance(ans.get("assets"), dict) else {}
    got = {k: str(v).strip().upper() for k, v in assets.items()}
    miss = [v["depth"] for d, v in key["retrieve"].items() if got.get(d) != v["code"]]
    upd_ok = sum(got.get(d) == v["code"] for d, v in key["update"].items())
    stale = sum(got.get(d) == v["old"] for d, v in key["update"].items())
    s = {"parsed": True,
         "retrieve": 1 - len(miss) / len(key["retrieve"]),
         "update": upd_ok / len(key["update"]),
         "count": float(str(ans.get("count")).strip() == str(key["count"])),
         "sign": float(str(ans.get("sign")).strip() == key["sign"]),
         "retrieve_miss_depths": miss, "update_stale": stale, "count_given": ans.get("count")}
    s["composite"] = round(sum(WEIGHTS[k] * s[k] for k in WEIGHTS), 4)
    return s

# ---------------------------------------------------------------- 校准

def run_tokens(cli, model, variant, message):
    target = model + (f"#{variant}" if variant else "")
    out = subprocess.run([cli, "run", "--format", "json", "--archived", "-m", target, message],
                         capture_output=True, text=True, encoding="utf-8", errors="replace",
                         cwd=os.environ.get("TEMP") or os.environ.get("TMPDIR") or "/tmp").stdout
    tok = 0
    for ln in out.splitlines():
        try:
            ev = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "step_finish":
            t = ev["part"]["tokens"]
            tok = max(tok, t["input"] + t["cache"]["read"] + t["cache"]["write"])
    return tok


def load_calib():
    raw = json.loads(CALIB.read_text("utf-8")) if CALIB.exists() else {}
    return {norm(k): v for k, v in raw.items()}


def calibrate(models):
    """两次 opencode run（短消息 / 约 1.3 万字符样本）的输入 token 之差 ÷ 字符数 = 该模型的 token/字符 比例。"""
    cli = find_cli()
    sample, _ = build(8000, 0.6, seed=12345)
    sample = sample.replace("\n", " ")  # 单行，避开命令行换行问题；长度低于 Windows 命令行上限
    tail = " 只回复两个字：收到"
    calib = load_calib()
    for m in models:
        v = m.get("calibration_variant", m["variant"])
        base = run_tokens(cli, m["model"], v, "你好" + tail)
        full = run_tokens(cli, m["model"], v, sample + tail)
        if not base or full <= base:
            print(f"{m['model']}: 校准失败 base={base} full={full}")
            continue
        calib[m["model"]] = round((full - base) / (len(sample) - 2), 4)
        print(f"{m['model']}: {calib[m['model']]} token/字符  (基线 {base}，样本 {full}，{len(sample)} 字符)", flush=True)
    RESULTS.mkdir(exist_ok=True)
    CALIB.write_text(json.dumps(calib, indent=2, ensure_ascii=False), "utf-8")

# ---------------------------------------------------------------- 运行

LOCK = threading.Lock()


def load_records():
    if not RAW.exists():
        return []
    recs = [json.loads(l) for l in RAW.read_text("utf-8").splitlines() if l.strip()]
    for j in recs:
        j["model"] = norm(j["model"])
    return recs


TRANSIENT = re.compile(r"HTTP 5\d\d|HTTP 429|timed out|Connection|Remote end closed", re.I)
CTX_ERR = re.compile(r"context|too long|maximum|exceed|token", re.I)


def run_cell(m, rung, trial, url, auth, ratio, timeout):
    model, variant = m["model"], m["variant"]
    prompt, key = build(rung, ratio, seed=rung * 10 + trial)
    t0 = time.time()
    for attempt in range(3):
        text, err = generate(url, auth, model, variant, prompt, timeout)
        retry = (err and TRANSIENT.search(err) and not CTX_ERR.search(err)) or (err is None and not (text or "").strip())
        if not retry or attempt == 2:
            break
        time.sleep(30)
    chars = len(prompt)
    del prompt  # 900K 档的归档有几 MB，并发时尽早释放
    rec = {"model": model, "variant": variant, "rung": rung, "trial": trial, "chars": chars,
           "secs": round(time.time() - t0, 1), "date": _date.today().isoformat(),
           "error": err, "answer": text, "key": key, "score": score(text, key) if err is None else None}
    with LOCK:
        with RAW.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        s = rec["score"]
        print(f"{model:40s} {rung:>9,} #{trial} {rec['secs']:>7.1f}s  " +
              (f"ERROR {err[:200]}" if err else
               f"综合 {s['composite']:.2f}  检索 {s['retrieve']:.1f} 更新 {s['update']:.2f} "
               f"计数 {s['count']:.0f}({s['count_given']}/{key['count']}) 规则 {s['sign']:.0f}"), flush=True)


def run(models, rungs, trials, timeout, concurrency, skip=()):
    calib = load_calib()
    missing = [m["model"] for m in models if m["model"] not in calib]
    if missing:
        sys.exit(f"缺少校准数据，先运行: python ctxbench.py calibrate --models {' '.join(missing)}")
    url, auth = server()
    RESULTS.mkdir(exist_ok=True)
    done = {(j["model"], j["rung"], j["trial"]) for j in load_records() if j.get("error") is None} | set(skip)
    # 所有 (模型, 档位, 次数) 放进同一个池子；按档位从大到小、模型交错排队，长请求先发出去
    cells = [(m, rung, trial) for rung in rungs for trial in range(trials) for m in models
             if (m["model"], rung, trial) not in done and not (m.get("max_context") and rung > m["max_context"])]
    if not cells:
        print("没有待跑的格子。")
        return
    workers = len(cells) if concurrency <= 0 else min(concurrency, len(cells))
    print(f"待跑 {len(cells)} 格，并发 {workers}", flush=True)
    with ThreadPoolExecutor(workers) as ex:
        for f in [ex.submit(run_cell, m, rung, trial, url, auth, calib[m["model"]], timeout) for m, rung, trial in cells]:
            f.result()

# ---------------------------------------------------------------- 汇总

def summarize(cfg):
    """按 (模型, 档位) 汇总。同一格子有重跑时，成功结果优先，其次取最后一次。"""
    latest = {}
    for j in load_records():
        k = (j["model"], j["rung"], j["trial"])
        if k not in latest or j["error"] is None or latest[k]["error"] is not None:
            latest[k] = j
    out = []
    for m in cfg["models"]:
        cells = {}
        for (mm, rung, _), j in latest.items():
            if mm == m["model"]:
                cells.setdefault(rung, []).append(j)
        rows = []
        for rung in sorted(cells):
            ok = [j for j in cells[rung] if j["error"] is None]
            row = {"rung": rung, "n": len(ok), "errors": [j["error"] for j in cells[rung] if j["error"]]}
            if ok:
                row.update({f: sum(j["score"][f] for j in ok) / len(ok) for f in ("composite", *WEIGHTS)})
                row["worst"] = min(j["score"]["composite"] for j in ok)
                row["secs"] = sum(j["secs"] for j in ok) / len(ok)
                row["miss"] = sorted(d for j in ok for d in j["score"]["retrieve_miss_depths"])
                row["stale"] = sum(j["score"]["update_stale"] for j in ok)
                row["empty"] = sum(not (j["answer"] or "").strip() for j in ok)
                row["unparsed"] = sum(not j["score"]["parsed"] for j in ok) - row["empty"]
            rows.append(row)

        by = {r["rung"]: r for r in rows}
        ladder = sorted(set(cfg["rungs"]) | set(by))
        if m.get("max_context"):
            ladder = [x for x in ladder if x <= m["max_context"]]

        def largest(th_avg, th_worst):  # 自己和所有更小档位都达标的最大档位；中间有没测的档位就停
            best = None
            for rung in ladder:
                r = by.get(rung)
                if not r or "composite" not in r or r["composite"] < th_avg or r["worst"] < th_worst:
                    break
                best = rung
            return best
        fallback = time.strftime("%Y-%m-%d", time.localtime(RAW.stat().st_mtime)) if RAW.exists() else None
        dates = sorted(j.get("date") or fallback for (mm, *_), j in latest.items() if mm == m["model"])
        out.append({**m, "rows": rows, "perfect": largest(0.999, 0.999), "usable": largest(PASS_AVG, PASS_WORST),
                    "date": dates[-1] if dates else None})
    return out


def k(n):
    return f"{n / 1_000_000:g}M" if n >= 1_000_000 else f"{n / 1000:g}K"

# ---------------------------------------------------------------- 折线图（纯 SVG，无依赖）

LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def chart_svg(summary):
    series = [s for s in summary if any("composite" in r for r in s["rows"])][:8]  # 最多 8 种分类色，不循环取色
    rungs = sorted({r["rung"] for s in series for r in s["rows"] if "composite" in r})
    W, H = 820, 440
    L, R, T, B = 56, 170, 76, 54
    pw, ph = W - L - R, H - T - B
    lo, hi = (math.log(rungs[0]), math.log(rungs[-1])) if rungs else (0, 1)
    X = lambda v: L + (pw * (math.log(v) - lo) / (hi - lo) if hi > lo else pw / 2)
    Y = lambda v: T + ph * (1 - v)
    css = [".bg{fill:#fcfcfb}.grid{stroke:#e1e0d9;stroke-width:1}.axis{stroke:#c3c2b7;stroke-width:1}"
           ".t1{fill:#0b0b0b}.t2{fill:#52514e}.t3{fill:#898781}.thr{stroke:#898781;stroke-width:1;stroke-dasharray:4 4}"
           ".ring{stroke:#fcfcfb}"]
    css += [f".s{i}{{stroke:{c}}}.f{i}{{fill:{c}}}" for i, c in enumerate(LIGHT)]
    dark = [".bg{fill:#1a1a19}.grid{stroke:#2c2c2a}.axis{stroke:#383835}.t1{fill:#f0efec}.t2{fill:#c3c2b7}.ring{stroke:#1a1a19}"]
    dark += [f".s{i}{{stroke:{c}}}.f{i}{{fill:{c}}}" for i, c in enumerate(DARK)]
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
         f'font-family="system-ui,-apple-system,\'Segoe UI\',\'PingFang SC\',\'Microsoft YaHei\',sans-serif">',
         f"<style>{''.join(css)}@media (prefers-color-scheme:dark){{{''.join(dark)}}}</style>",
         f'<rect class="bg" width="{W}" height="{H}" rx="8"/>',
         f'<text class="t1" x="{L}" y="28" font-size="16" font-weight="600">综合分 vs 上下文长度</text>',
         f'<text class="t2" x="{L}" y="47" font-size="12">越靠右越长；线越平越稳。虚线 = 可用门槛 {PASS_AVG:.2f}</text>']
    # 图例
    lx = L
    for i, s in enumerate(series):
        o.append(f'<line class="s{i}" x1="{lx}" y1="62" x2="{lx + 16}" y2="62" stroke-width="2"/>'
                 f'<circle class="f{i} ring" cx="{lx + 8}" cy="62" r="4" stroke-width="2"/>'
                 f'<text class="t2" x="{lx + 22}" y="66" font-size="12">{esc(s["name"])}</text>')
        lx += 30 + 7 * len(s["name"].encode("gbk", "replace")) // 1
    # 网格与坐标轴
    for v in (0, 0.2, 0.4, 0.6, 0.8, 1.0):
        o.append(f'<line class="{"axis" if v == 0 else "grid"}" x1="{L}" y1="{Y(v):.1f}" x2="{L + pw}" y2="{Y(v):.1f}"/>'
                 f'<text class="t3" x="{L - 8}" y="{Y(v) + 4:.1f}" font-size="11" text-anchor="end">{v:.1f}</text>')
    o.append(f'<line class="thr" x1="{L}" y1="{Y(PASS_AVG):.1f}" x2="{L + pw}" y2="{Y(PASS_AVG):.1f}"/>')
    shown, last = [], 1e9
    for v in reversed(rungs):  # 从右往左放刻度标签，挤在一起的跳过
        if last - X(v) >= 38:
            shown.append(v)
            last = X(v)
    for v in rungs:
        o.append(f'<line class="axis" x1="{X(v):.1f}" y1="{T + ph}" x2="{X(v):.1f}" y2="{T + ph + 4}"/>')
        if v in shown:
            o.append(f'<text class="t3" x="{X(v):.1f}" y="{T + ph + 18}" font-size="11" text-anchor="middle">{k(v)}</text>')
    o.append(f'<text class="t3" x="{L + pw / 2}" y="{H - 12}" font-size="11" text-anchor="middle">上下文长度（token，对数刻度）</text>')
    # 折线、点、末端标签
    ends = []
    for i, s in enumerate(series):
        pts = [(X(r["rung"]), Y(r["composite"])) for r in s["rows"] if "composite" in r]
        o.append(f'<polyline class="s{i}" fill="none" stroke-width="2" stroke-linejoin="round" '
                 f'points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in pts)}"/>')
        o += [f'<circle class="f{i} ring" cx="{x:.1f}" cy="{y:.1f}" r="4" stroke-width="2"/>' for x, y in pts]
        last_row = [r for r in s["rows"] if "composite" in r][-1]
        ends.append([pts[-1][1], i, f'{s["name"]} {last_row["composite"]:.2f}', pts[-1][0]])
    ends.sort()
    for a, b in zip(ends, ends[1:]):  # 末端标签纵向避让
        b[0] = max(b[0], a[0] + 15)
    for y, i, label, x in ends:
        o.append(f'<text class="t2" x="{L + pw + 10}" y="{y + 4:.1f}" font-size="12">{esc(label)}</text>')
    o.append("</svg>")
    return "\n".join(o)

# ---------------------------------------------------------------- 报告与 README

def leaderboard_md(summary, cfg):
    ranked = sorted(summary, key=lambda s: (s["usable"] or 0, s["perfect"] or 0,
                                            sum(r.get("composite", 0) for r in s["rows"])), reverse=True)
    rungs = sorted({r["rung"] for s in summary for r in s["rows"]}, reverse=True)
    md = ["| # | 模型 | 思考档 | 推荐上下文<br>（全部满分） | 可用上限<br>（均分≥0.90 且单次≥0.80） | 每档次数 | 测试日期 |",
          "|---:|---|---|---:|---:|---:|---|"]
    fmt = lambda v: k(v) if v else "< 最小档"
    typical = {s["model"]: max((r["n"] for r in s["rows"]), default=0) for s in summary}
    for n, s in enumerate(ranked, 1):
        md.append(f"| {n} | {s['name']}<br><sub>`{s['model']}`</sub> | {s['variant'] or '默认'} | "
                  f"**{fmt(s['perfect'])}** | **{fmt(s['usable'])}** | {typical[s['model']]} | {s['date'] or '-'} |")
    md += ["", "各档综合分（每格为各次试验的平均；— 表示未测或请求失败）：", "",
           "| 模型 | " + " | ".join(k(r) for r in rungs) + " |", "|---|" + "---:|" * len(rungs)]
    md[-4] = "各档综合分（每格为各次试验的平均；— 表示未测或请求失败；<sup>×n</sup> 表示该格次数少于该模型的常规次数）："
    for s in ranked:
        by = {r["rung"]: r for r in s["rows"]}
        cells = []
        for r in rungs:
            row = by.get(r)
            if not row or "composite" not in row:
                cells.append("—")
            else:
                cells.append(f"{row['composite']:.2f}" + ("" if row["n"] >= typical[s["model"]] else f"<sup>×{row['n']}</sup>"))
        md.append(f"| {s['name']} | " + " | ".join(cells) + " |")
    return "\n".join(md)


def report():
    cfg = load_config()
    summary = summarize(cfg)
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "chart.svg").write_text(chart_svg(summary), "utf-8")
    board = leaderboard_md(summary, cfg)

    out = ["# 详细结果", "", f"综合分 = 检索×{WEIGHTS['retrieve']} + 更新×{WEIGHTS['update']} + 计数×{WEIGHTS['count']} + 开头规则×{WEIGHTS['sign']}。", "",
           "![chart](chart.svg)", "", board, ""]
    for s in summary:
        if not s["rows"]:
            continue
        out += [f"## {s['name']}（`{s['model']}`，思考档 {s['variant'] or '默认'}）", "",
                "| 上下文 | 次数 | 综合 | 检索 | 更新 | 计数 | 规则 | 最差一次 | 平均耗时 | 备注 |",
                "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
        for r in sorted(s["rows"], key=lambda r: -r["rung"]):
            errs = [e[:80].replace("|", "/").replace("\n", " ") for e in r["errors"]]
            if "composite" not in r:
                out.append(f"| {k(r['rung'])} | 0 | - | - | - | - | - | - | - | 请求失败：{errs[0] if errs else ''} |")
                continue
            note = []
            if r["miss"]:
                note.append("检索漏掉深度 " + ",".join(f"{d:.0%}" for d in r["miss"]))
            if r["stale"]:
                note.append(f"用了旧码 {r['stale']} 次")
            if r["empty"]:
                note.append(f"空回答 {r['empty']} 次")
            if r["unparsed"]:
                note.append(f"格式不对 {r['unparsed']} 次")
            if errs:
                note.append(f"另有 {len(errs)} 次请求失败")
            out.append(f"| {k(r['rung'])} | {r['n']} | {r['composite']:.2f} | {r['retrieve']:.2f} | {r['update']:.2f} | "
                       f"{r['count']:.2f} | {r['sign']:.2f} | {r['worst']:.2f} | {r['secs']:.0f}s | {'；'.join(note)} |")
        out.append("")
    (RESULTS / "report.md").write_text("\n".join(out), "utf-8")

    if README.exists():
        text = README.read_text("utf-8")
        new = re.sub(r"(<!-- LEADERBOARD:START -->).*?(<!-- LEADERBOARD:END -->)",
                     lambda m: f"{m.group(1)}\n![综合分 vs 上下文长度](results/chart.svg)\n\n{board}\n\n"
                               f"逐档明细（检索漏掉的深度、空回答、耗时等）见 [results/report.md](results/report.md)。\n{m.group(2)}",
                     text, flags=re.S)
        README.write_text(new, "utf-8")
    print(board)

# ---------------------------------------------------------------- 入口

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="长上下文注意力阶梯测试（经由 opencode）")
    p.add_argument("cmd", choices=["calibrate", "run", "report"])
    p.add_argument("--models", nargs="+", help="只跑这些模型（provider/model），默认 config.json 里的全部")
    p.add_argument("--rungs", nargs="+", type=int, help="覆盖 config.json 的阶梯")
    p.add_argument("--trials", type=int, help="覆盖 config.json 的每档次数")
    p.add_argument("--concurrency", type=int, help="覆盖 config.json 的并发数（0 = 所有格子同时发出）")
    p.add_argument("--skip", nargs="+", default=[], metavar="MODEL@RUNG#TRIAL",
                   help="本次跳过的格子，例如 opencode-go/glm-5.3-flash@400000#1（别的进程正在跑它时用）")
    a = p.parse_args()
    cfg = load_config()
    models = cfg["models"]
    if a.models:
        known = {m["model"]: m for m in models}
        models = [known.get(x) or {"model": x, "variant": None, "name": x.split("/", 1)[1]} for x in a.models]
    if a.cmd == "calibrate":
        calibrate(models)
    elif a.cmd == "run":
        skip = []
        for s in a.skip:
            mm, rest = s.rsplit("@", 1)
            rung, trial = rest.split("#")
            skip.append((norm(mm), int(rung), int(trial)))
        conc = a.concurrency if a.concurrency is not None else cfg.get("concurrency", 8)
        run(models, sorted(a.rungs or cfg["rungs"], reverse=True), a.trials or cfg["trials"],
            cfg.get("request_timeout_s", 3600), conc, skip)
    else:
        report()
