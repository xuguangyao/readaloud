#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
readaloud_web.py —— 朗读工作台（本地网页版）

    python readaloud_web.py [--port 8765]

打开后可以做四件事：
  1. 浏览本机目录，点选 PDF / txt / md
  2. 粘贴网页地址抓正文
  3. 直接把文件拖进页面上传
  4. 读剪贴板（内网文章 Ctrl+C 之后点一下）

转换完在同一个页面里播放，句子跟着高亮。
只监听 127.0.0.1，不对外网开放。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import socket
import string
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import readaloud as R  # noqa: E402

UI_PATH = SCRIPT_DIR / "web_ui.html"
OUT_DIR = R.DEFAULT_OUTDIR
UPLOAD_DIR = SCRIPT_DIR / "uploads"
ALLOW_EXT = {".pdf", ".txt", ".md", ".markdown", ".html", ".htm"}

# 允许浏览的目录。手动输入路径不受此限制。
ALLOW_ROOTS: list[Path] = []
for _p in [Path.home() / "Downloads", Path.home() / "Documents", Path.home() / "Desktop",
           SCRIPT_DIR / "samples", SCRIPT_DIR / "output"]:
    if _p.exists():
        ALLOW_ROOTS.append(_p)
for _d in ("C:\\", "D:\\", "E:\\", "F:\\"):      # 必须带分隔符，否则是盘符相对路径
    _w = Path(_d) / "aiworkplace"
    if _w.is_dir():
        ALLOW_ROOTS.append(_w)
        break

# 本服务只监听 127.0.0.1（只有本机浏览器能访问），因此默认放开目录浏览：
# 任意目录都可以在网页里点选。想恢复原来的白名单限制，改成 False 即可。
BROWSE_ANY = True


def all_drives() -> list[str]:
    """枚举本机所有存在的盘符，作为网页浏览入口。"""
    return [f"{ch}:\\" for ch in string.ascii_uppercase
            if os.path.isdir(f"{ch}:\\")]

_orig_log = R.log

# ---------------------------------------------------------------- 任务状态

TASKS: dict[str, dict] = {}
TASK_LOCK = threading.Lock()
VOICE_CACHE: dict = {}
CHAPTER_CACHE: dict[str, dict] = {}        # 章节识别结果（按 路径+mtime 缓存）


def new_task(name: str) -> dict:
    t = {
        "id": f"{int(time.time() * 1000):x}",
        "name": name,
        "state": "queued",          # queued / running / done / error / cancelled
        "stage": "排队中",
        "percent": 0.0,
        "log": [],
        "result": None,
        "error": None,
        "cancel": False,            # 取消请求标志，合成循环每帧查一次
        "started": time.time(),
    }
    TASKS[t["id"]] = t
    for k, v in list(TASKS.items()):          # 只留最近 20 个
        if v["state"] in ("done", "error") and time.time() - v["started"] > 3600:
            TASKS.pop(k, None)
    return t


def make_task_logger(t: dict):
    """把 readaloud 的进度输出翻译成百分比。"""

    def _log(msg: str = "") -> None:
        s = str(msg).strip()
        if not s:
            return
        t["log"].append(s)
        del t["log"][:-60]
        m = re.search(r"\[(\d+)/(\d+)\]", s)
        if m:
            done, tot = int(m.group(1)), int(m.group(2))
            t["percent"] = max(t["percent"],
                               round(5 + 92 * min(done, tot) / max(1, tot), 1))
            t["stage"] = f"合成语音 {done}/{tot} 段"
        elif s.startswith("①"):
            t["stage"], t["percent"] = "解析内容", 2.0
        elif "正文" in s and "字" in s:
            t["stage"], t["percent"] = "抽取正文", 5.0
        elif "合成语音" in s:
            t["stage"], t["percent"] = "合成语音中（首次建连约需十几秒）", max(t["percent"], 8.0)
        elif s.startswith("③") or "完成" in s:
            t["percent"] = 99.0
        elif s.startswith("×") or s.startswith("!"):
            t["stage"] = s[:40]

    return _log


_EXTRACT_CACHE: dict[str, dict] = {}          # 抽取统计缓存（大 PDF 抽一次要几秒）


def _extract_stats(target: str, pages) -> dict:
    key = f"{target}|{pages or ''}"
    hit = _EXTRACT_CACHE.get(key)
    if hit:
        return hit
    if (target or "").startswith("text:"):
        title, paras, info = R.extract_pasted(target[5:], "手工粘贴")
    else:
        title, paras, info = R.load_source(target, pages)
    if not paras:
        raise SystemExit("没有抽到正文。可能是扫描版 PDF（整页是图片，需先 OCR）"
                         "或纯 JS 渲染页面。")
    st = {"title": title, "type": info["source_type"], "where": info.get("sitename", ""),
          "paras": len(paras), "full": sum(len(p) for p in paras)}
    if len(_EXTRACT_CACHE) > 12:
        _EXTRACT_CACHE.clear()
    _EXTRACT_CACHE[key] = st
    return st


def preview(opt: dict) -> dict:
    """只做正文抽取、不合成，用来在点「开始朗读」前告诉用户到底会读多少。"""
    pages = R.parse_pages(opt.get("pages", ""))
    txt = str(opt.get("text") or "").strip()
    tgt = str(opt.get("target") or "").strip()
    if not txt and not tgt:
        raise SystemExit("没有指定内容来源")
    target = "text:" + txt if txt else tgt
    st = _extract_stats(target, pages)
    full = st["full"]
    limit = int(opt.get("max_chars") or 0)
    used = full if limit <= 0 else min(full, limit)
    rate = str(opt.get("rate") or "+0%")
    m = re.fullmatch(r"([+-])(\d+)%", rate.strip())
    speed = 1 + (int(m.group(2)) * (1 if m.group(1) == "+" else -1) / 100) if m else 1
    return {
        "title": st["title"],
        "type": st["type"],
        "where": st["where"],
        "paras": st["paras"],
        "full_chars": full,
        "used_chars": used,
        "truncated": used < full,
        "audio_min": round(used / 320 / max(0.3, speed), 1),      # 实测约 320 字/分钟
        "synth_sec": round(used / 153, 0),                        # 实测约 153 字/秒
        "mp3_mb": round(used / 320 * 60 * 6000 / 1048576 / max(0.3, speed), 1),
    }


def run_task(t: dict, opt: dict) -> None:
    workdir = None
    try:
        t["state"] = "running"
        R.log = make_task_logger(t)

        pages = R.parse_pages(opt.get("pages", ""))
        R.log(f"① 解析内容：{opt.get('target') or '粘贴的文本'}")
        if (opt.get("text") or "").strip():
            title, paras, info = R.extract_pasted(opt["text"], "手工粘贴")
        else:
            title, paras, info = R.load_source(opt["target"], pages)
        if not paras:
            raise SystemExit("没有抽到正文。可能是扫描版 PDF（整页是图片）或纯 JS 渲染页面。")

        total0 = sum(len(p) for p in paras)
        limit = int(opt.get("max_chars") or 0)
        if limit < 0 and total0 > R.SOFT_LIMIT:
            limit = R.SOFT_LIMIT
            R.log(f"! 文档较大（{total0} 字），已截断前 {limit} 字；改「字数上限」可读更多")
        if limit > 0 and total0 > limit:
            acc, cut = 0, []
            for p in paras:
                acc += len(p)
                cut.append(p)
                if acc >= limit:
                    break
            paras = cut

        total = sum(len(p) for p in paras)
        R.log(f"   标题：{title}")
        R.log(f"   段落 {len(paras)} 个 / 正文 {total} 字 / 来源 {info['source_type']}")

        structured = [[list([s, 0.0]) for s in R.split_sentences(p)] for p in paras]

        workdir = OUT_DIR / R.slugify(title, info["source"], opt.get("pages", "") or "")
        workdir.mkdir(parents=True, exist_ok=True)
        voice = opt.get("voice") or R.DEFAULT_VOICE
        rate = opt.get("rate") or "+0%"
        pitch = opt.get("pitch") or "+0Hz"

        R.log(f"② 合成语音（{voice}，语速 {rate}）")
        t["percent"] = 8.0
        audio, dur_s = asyncio.run(R.synthesize(
            structured, workdir, voice, rate, pitch,
            should_cancel=lambda: t["cancel"]))

        data = R.build_payload(title, structured, info, voice, rate, dur_s, total)
        (workdir / "article.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        (workdir / "index.html").write_text(
            R.render_html(data, f"/media/{urllib.parse.quote(workdir.name)}/article.mp3"),
        encoding="utf-8")

        t["percent"] = 100.0
        t["state"] = "done"
        t["stage"] = "完成"
        t["result"] = {
            "slug": workdir.name,
            "title": title,
            "words": total,
            "duration": round(dur_s, 1),
            "url": f"/read/{urllib.parse.quote(workdir.name)}",
        }
    except R.Cancelled:                            # 用户主动取消
        t["state"], t["stage"] = "cancelled", "已取消"
        t["percent"], t["error"] = 0.0, None
        # 若这是本次新建的空目录，删掉，别在 output 下留垃圾
        try:
            if workdir and workdir.is_dir() and not any(workdir.iterdir()):
                workdir.rmdir()
        except OSError:
            pass
    except SystemExit as e:                      # readaloud 用 SystemExit 报错
        t["state"], t["error"] = "error", str(e).strip() or "未知错误"
    except Exception as e:                        # noqa: BLE001
        t["state"], t["error"] = "error", f"{type(e).__name__}: {e}"
        traceback.print_exc()
    finally:
        R.log = _orig_log
        if t["state"] == "error":
            t["stage"] = "失败"


# ---------------------------------------------------------------- 目录浏览

def in_allowed(p: Path) -> bool:
    if BROWSE_ANY:
        return True
    try:
        rp = str(p.resolve()).lower().rstrip("\\/")
    except OSError:
        return False
    for r in ALLOW_ROOTS:
        try:
            rr = str(r.resolve()).lower().rstrip("\\/")
        except OSError:
            continue
        if rp == rr or rp.startswith(rr + os.sep) or rp.startswith(rr + "/"):
            return True
    return False


def human_size(n: int) -> str:
    if n >= 1048576:
        return f"{n / 1048576:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def list_dir(d: Path) -> dict:
    dirs, files = [], []
    try:
        entries = sorted(d.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    except OSError as e:
        raise SystemExit(f"无法读取目录：{e}")
    for p in entries:
        try:
            if p.name.startswith((".", "$")) or p.name.lower() in ("desktop.ini",):
                continue
            if p.is_dir():
                dirs.append(p.name)
            elif p.suffix.lower() in ALLOW_EXT:
                st = p.stat()
                files.append({
                    "name": p.name,
                    "size": human_size(st.st_size),
                    "mtime": time.strftime("%Y-%m-%d", time.localtime(st.st_mtime)),
                })
        except OSError:
            continue
    parent = d.parent if str(d.parent) != str(d) else None
    return {
        "path": str(d),
        "parent": str(parent) if parent else "",
        "allowed": in_allowed(d),
        "dirs": dirs[:200],
        "files": files[:300],
    }


def recent_files() -> list[dict]:
    out, seen = [], set()
    for r in ALLOW_ROOTS:
        if r.name == "output":
            continue
        try:
            cand = list(r.glob("*"))
            for sub in r.glob("*"):
                if sub.is_dir():
                    cand.extend(sub.glob("*"))
        except OSError:
            continue
        for p in cand:
            try:
                if not p.is_file() or p.suffix.lower() not in ALLOW_EXT:
                    continue
                if str(p) in seen:
                    continue
                seen.add(str(p))
                st = p.stat()
                out.append({"path": str(p), "name": p.name,
                            "mtime": st.st_mtime,
                            "when": time.strftime("%m-%d", time.localtime(st.st_mtime))})
            except OSError:
                continue
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out[:12]


def library() -> list[dict]:
    items = []
    if not OUT_DIR.is_dir():
        return items
    for d in sorted(OUT_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        jf = d / "article.json"
        if not d.is_dir() or not jf.exists():
            continue
        try:
            j = json.loads(jf.read_text(encoding="utf-8"))
        except Exception:                                      # noqa: BLE001
            continue
        items.append({
            "slug": d.name,
            "url": f"/read/{urllib.parse.quote(d.name)}",
            "title": j.get("title", d.name),
            "words": j.get("words", 0),
            "duration": j.get("duration", 0),
            "voice": (j.get("voice") or "").replace("Neural", ""),
            "created": j.get("created", ""),
            "src": (j.get("info") or {}).get("source", ""),
            "type": (j.get("info") or {}).get("source_type", ""),
        })
    return items


def safe_slug(slug: str) -> Path | None:
    """只允许 output 下的直接子目录，防目录穿越。"""
    p = (OUT_DIR / slug).resolve()
    try:
        p.relative_to(OUT_DIR.resolve())
    except ValueError:
        return None
    return p if p.is_dir() else None


# ---------------------------------------------------------------- HTTP

# 客户端中途断开：浏览器 seek / 暂停 / 切页 / 关标签，会丢弃正在下载的音频连接。
# 注意 ConnectionAbortedError(WinError 10053) 属于 ConnectionError，但它既不是
# BrokenPipeError 也不是 ConnectionResetError 的子类——只抓后两者会漏掉它。
CONN_ERRORS = (ConnectionError, TimeoutError)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ReadAloudWorkbench/1.0"
    timeout = 30                       # 避免 keep-alive 线程永久挂起

    def handle(self):
        # 浏览器预取/切页会随时掐断 keep-alive 连接，这类异常属正常现象
        try:
            super().handle()
        except (OSError, ConnectionError, TimeoutError):
            pass

    def log_message(self, fmt, *args):            # 静音访问日志
        pass

    # ---- 基础输出 ----
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except CONN_ERRORS:
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _err(self, msg: str, code: int = 400) -> None:
        self._json({"ok": False, "error": msg}, code)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    # ---- 路由 ----
    def do_GET(self):                           # noqa: N802
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        path = u.path
        try:
            if path in ("/", "/index.html"):
                self._send(200, UI_PATH.read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/roots":
                self._json({"ok": True, "roots": [str(p) for p in ALLOW_ROOTS],
                            "drives": all_drives(),
                            "cwd": str(Path.cwd())})
            elif path == "/api/browse":
                raw = (q.get("path") or [""])[0].strip()
                d = Path(raw) if raw else ALLOW_ROOTS[0]
                if not d.exists():
                    return self._err(f"目录不存在：{d}")
                if not in_allowed(d):
                    return self._err(f"为安全起见，网页里只能浏览这些目录：\n"
                                     f"{', '.join(str(p) for p in ALLOW_ROOTS)}\n"
                                     f"其它路径请在上方输入框直接填写完整路径。")
                self._json({"ok": True, **list_dir(d)})
            elif path == "/api/recent":
                self._json({"ok": True, "files": recent_files()})
            elif path == "/api/library":
                self._json({"ok": True, "items": library()})
            elif path == "/api/progress":
                t = TASKS.get((q.get("id") or [""])[0])
                if not t:
                    return self._err("任务不存在或已过期", 404)
                self._json({"ok": True, "name": t["name"], "state": t["state"],
                            "stage": t["stage"], "percent": t["percent"],
                            "cancel": t["cancel"], "log": t["log"][-6:],
                            "result": t["result"], "error": t["error"]})
            elif path == "/api/voices":
                self._json({"ok": True, "voices": get_voices()})
            elif path == "/api/clipboard":
                txt = R.read_clipboard()
                self._json({"ok": True, "text": txt, "chars": len(txt.strip())})
            elif path == "/api/preview":
                opt = json.loads((q.get("opt") or ["{}"])[0])
                self._json({"ok": True, **preview(opt)})
            elif path == "/api/chapters":
                self._chapters((q.get("path") or [""])[0].strip())
            elif path.startswith("/read/"):
                self._read_page(urllib.parse.unquote(path[len("/read/"):]))
            elif path.startswith("/media/"):
                self._media(urllib.parse.unquote(path[len("/media/"):]))
            else:
                self._err("not found", 404)
        except CONN_ERRORS:
            return                    # 客户端断开不是错误，别刷堆栈也别回写响应
        except SystemExit as e:
            self._err(str(e))
        except Exception as e:                   # noqa: BLE001
            traceback.print_exc()
            self._err(f"{type(e).__name__}: {e}", 500)

    def do_POST(self):                          # noqa: N802
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        path = u.path
        try:
            if path == "/api/make":
                opt = json.loads(self._body().decode("utf-8") or "{}")
                tgt = str(opt.get("target") or "").strip()
                txt = str(opt.get("text") or "").strip()
                if not tgt and not txt:
                    return self._err("没有指定内容来源")
                with TASK_LOCK:
                    busy = [t for t in TASKS.values() if t["state"] in ("queued", "running")]
                    if busy:
                        return self._err(f"正在处理《{busy[0]['name']}》，请等它完成或点取消", 409)
                    name = (Path(tgt).name if tgt else "") or f"粘贴文本 {len(txt)}字"
                    t = new_task(name[:60])
                threading.Thread(target=run_task, args=(t, opt), daemon=True).start()
                self._json({"ok": True, "id": t["id"]})
            elif path == "/api/cancel":
                opt = json.loads(self._body().decode("utf-8") or "{}")
                t = TASKS.get(str(opt.get("id", "")))
                if not t:
                    return self._err("任务不存在或已结束", 404)
                if t["state"] in ("done", "error", "cancelled"):
                    return self._json({"ok": True, "state": t["state"], "already": True})
                t["cancel"] = True
                t["stage"] = "正在取消…"
                self._json({"ok": True, "state": t["state"]})
            elif path == "/api/upload":
                fname = urllib.parse.unquote((q.get("name") or ["file.bin"])[0])
                fname = os.path.basename(fname)
                data = self._body()
                if not data:
                    return self._err("上传内容为空")
                if len(data) > 200 * 1048576:
                    return self._err("文件超过 200MB")
                UPLOAD_DIR.mkdir(exist_ok=True)
                stem, suf = os.path.splitext(fname)
                if suf.lower() not in ALLOW_EXT:
                    return self._err(f"只支持 {', '.join(sorted(ALLOW_EXT))}")
                target = UPLOAD_DIR / f"{stem}_{int(time.time()) % 100000}{suf}"
                target.write_bytes(data)
                self._json({"ok": True, "path": str(target), "name": fname,
                            "size": human_size(len(data))})
            elif path == "/api/delete":
                opt = json.loads(self._body().decode("utf-8") or "{}")
                d = safe_slug(str(opt.get("slug", "")))
                if not d:
                    return self._err("路径不合法")
                import shutil

                shutil.rmtree(d, ignore_errors=True)
                self._json({"ok": True})
            elif path == "/api/reveal":
                opt = json.loads(self._body().decode("utf-8") or "{}")
                d = safe_slug(str(opt.get("slug", "")))
                if not d:
                    return self._err("路径不合法")
                os.startfile(str(d))                     # noqa: S606
                self._json({"ok": True})
            else:
                self._err("not found", 404)
        except CONN_ERRORS:
            return                    # 客户端断开不是错误，别刷堆栈也别回写响应
        except SystemExit as e:
            self._err(str(e))
        except Exception as e:                   # noqa: BLE001
            traceback.print_exc()
            self._err(f"{type(e).__name__}: {e}", 500)

    def do_DELETE(self):                         # noqa: N802
        self.do_POST()

    # ---- 播放页与音频 ----
    def _chapters(self, raw: str) -> None:
        """识别 PDF 章节，给前端「看目录」面板用。"""
        if not raw:
            return self._err("先选一个 PDF 文件")
        p = Path(raw).expanduser()
        if not p.is_file():
            return self._err(f"文件不存在：{p.name or p}")
        if p.suffix.lower() != ".pdf":
            return self._err("只有 PDF 有页码概念，txt / md / 网页不用填页范围")
        try:
            st = p.stat()
            key = f"{p}|{int(st.st_mtime)}"
            res = CHAPTER_CACHE.get(key)
            if res is None:
                import booktoc

                res = booktoc.detect(p)
                if len(CHAPTER_CACHE) > 8:
                    CHAPTER_CACHE.clear()
                CHAPTER_CACHE[key] = res
            self._json({"ok": True, "path": str(p), **res})
        except Exception as e:                               # noqa: BLE001
            self._err(f"章节识别失败：{type(e).__name__}: {e}。"
                      f"这个 PDF 可能结构特殊或已损坏，可以直接按页数分段读。")

    def _read_page(self, slug: str) -> None:
        d = safe_slug(slug)
        jf = d / "article.json" if d else None
        if not jf or not jf.exists():
            return self._err("作品不存在，可能已被删除", 404)
        data = json.loads(jf.read_text(encoding="utf-8"))
        html = R.render_html(data, f"/media/{urllib.parse.quote(d.name)}/article.mp3")
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

    def _media(self, rel: str) -> None:
        parts = rel.split("/")
        if len(parts) != 2 or parts[1] not in ("article.mp3",):
            return self._err("bad request", 400)
        d = safe_slug(parts[0])
        f = d / parts[1] if d else None
        if not f or not f.exists():
            return self._err("file not found", 404)
        size = f.stat().st_size
        rng = self.headers.get("Range", "")
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)", rng)
        partial = False
        if m:
            g1, g2 = m.group(1), m.group(2)
            if g1 and g2:
                start, end = int(g1), min(int(g2), size - 1)
                partial = True
            elif g1:
                start, end = int(g1), size - 1
                partial = True
            elif g2:                              # bytes=-N → 最后 N 字节
                start, end = max(0, size - int(g2)), size - 1
                partial = True
            if start > end:
                start, end, partial = 0, size - 1, False
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with f.open("rb") as fh:
                fh.seek(start)
                remain = length
                while remain > 0:
                    buf = fh.read(min(1048576, remain))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    remain -= len(buf)
        except CONN_ERRORS:
            pass                                   # 对端已断开，剩余字节不必再发

    def do_HEAD(self):                           # noqa: N802
        self.send_response(200)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", "0")
        self.end_headers()


def get_voices() -> list[dict]:
    if VOICE_CACHE.get("data"):
        return VOICE_CACHE["data"]

    async def _fetch():
        import edge_tts

        return await edge_tts.list_voices()

    try:
        vs = asyncio.run(_fetch())
    except Exception:                                             # noqa: BLE001
        vs = []
    out = []
    for v in sorted(vs, key=lambda x: x.get("ShortName", "")):
        loc = str(v.get("Locale", ""))
        if not loc.lower().startswith("zh-"):
            continue
        out.append({"short": v.get("ShortName", ""),
                    "gender": "女" if v.get("Gender") == "Female" else "男",
                    "locale": loc})
    if not out:
        out = [{"short": R.DEFAULT_VOICE, "gender": "女", "locale": "zh-CN"}]
    VOICE_CACHE["data"] = out
    return out


def free_port(preferred: int) -> int:
    for p in (preferred, 0):
        try:
            s = socket.socket()
            s.bind(("127.0.0.1", p))
            port = s.getsockname()[1]
            s.close()
            return port
        except OSError:
            continue
    return preferred


def already_running(port: int) -> bool:
    """端口上已有本工作台 → 直接复用，避免重复启动第二个服务。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/roots", timeout=2) as r:
            return b"roots" in r.read()
    except Exception:                                       # noqa: BLE001
        return False


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                       # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="朗读工作台（本地网页版）")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    if already_running(args.port):
        url = f"http://127.0.0.1:{args.port}/"
        print(f"工作台已在运行，直接打开：{url}")
        print("（这个窗口可以关掉，不影响正在运行的服务）")
        if not args.no_browser:
            webbrowser.open(url)
        return

    OUT_DIR.mkdir(exist_ok=True)
    port = free_port(args.port)
    url = f"http://127.0.0.1:{port}/"
    ThreadingHTTPServer.allow_reuse_address = True
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("=" * 58)
    print(f"  朗读工作台已启动：  {url}")
    print(f"  可浏览目录：{', '.join(str(p) for p in ALLOW_ROOTS[:3])} …")
    print("  按 Ctrl+C 停止")
    print("=" * 58, flush=True)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
