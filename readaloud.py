#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
readaloud.py —— 把「网页 / PDF / txt / md」转成可同步高亮的有声读物。

用法:
    python readaloud.py <URL 或 文件路径> [选项]

常用:
    python readaloud.py https://example.com/article
    python readaloud.py D:\\docs\\report.pdf --rate +15%
    python readaloud.py notes.md --voice zh-CN-YunxiNeural --no-open
    python readaloud.py list                       # 列出中文音色
    python readaloud.py <path> --serve             # 起本地服务打开(比双击更稳)

产物: output/<slug>/  下有 article.mp3 + index.html(内嵌全文与时间轴，双击即听)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import json
import os
import re
import sys
import time
import webbrowser
from pathlib import Path

# ---------------------------------------------------------------- 常量配置

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
MAX_CHUNK_CHARS = 1600          # 单次 TTS 请求的字符上限
MAX_SENTENCE_CHARS = 120        # 超长句强制再切
SCRIPT_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = SCRIPT_DIR / "player.html"
DEFAULT_OUTDIR = SCRIPT_DIR / "output"
SOFT_LIMIT = 20000              # 超大文档默认截断字数（约 80 分钟音频）

# 句子终止符（中英混合）
SENT_END = "。！？!?；;…"
SENT_KEEP_AFTER = "”’\"')）》」』】"
CN_SPLIT = "，,、：: "

# 归一化用：只保留汉字/字母/数字，用于把 TTS 词时间戳映射回句子
NORMALIZE_KEEP = re.compile(r"[0-9A-Za-z\u4e00-\u9fff\u3400-\u4dbf]")

# 需要剔除的噪声行（页眉页脚、页码、水印、目录引导线）
NOISE_PATTERNS = [
    re.compile(r"^\s*[-–—]\s*\d+\s*[-–—]\s*$"),
    re.compile(r"^\s*(第?\s*\d+\s*页|Page\s*\d+(\s*/\s*\d+)?|\d+\s*/\s*\d+)\s*$", re.I),
    re.compile(r"^\s*\d{1,4}\s*$"),
    re.compile(r"^\s*[ivxlcdm]{1,8}\s*$", re.I),          # 前置页罗马数字页码
    re.compile(r"^\s*(?:[.·]\s*){3,}\d*\s*$"),             # 纯目录引导线
]
# 目录条目特征：正文里出现「. . . . .」这种引导线基本只可能是目录
TOC_LEADER = re.compile(r"(?:[.·]\s){3,}")
# 图表坐标轴刻度（纯数字与小数点），朗读无意义
AXIS_NUMS = re.compile(r"^\s*[\d\s\.\-–—%]{1,30}\s*$")


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- 1. 正文抽取

def is_noise(text: str) -> bool:
    t = text.strip()
    if not t:
        return True
    if any(rx.match(t) for rx in NOISE_PATTERNS):
        return True
    # 「标题 . . . . . 页码」= 目录行，朗读它毫无意义
    if TOC_LEADER.search(t) and re.search(r"\d+\s*$", t):
        return True
    return False

def read_clipboard() -> str:
    """读取 Windows 剪贴板（内网文章 / 需登录页面最实用的入口）。

    优先用 Win32 API 直读（毫秒级）；不可用时退回 PowerShell 子进程。
    """
    try:
        import ctypes
        from ctypes import wintypes

        CF_UNICODETEXT = 13
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        # 64 位下必须显式声明，否则 GetClipboardData 的句柄会被截断成 32 位
        user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
        user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
        user32.OpenClipboard.argtypes = [ctypes.c_void_p]
        user32.GetClipboardData.restype = ctypes.c_void_p
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.CloseClipboard.restype = wintypes.BOOL
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return ""
        if not user32.OpenClipboard(None):
            raise OSError("OpenClipboard 失败")
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            kernel32.GlobalLock.restype = ctypes.c_void_p
            kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
            kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()
    except Exception:                                       # noqa: BLE001
        import subprocess

        try:
            p = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                capture_output=True, timeout=8, stdin=subprocess.DEVNULL,
            )
        except Exception:                                   # noqa: BLE001
            return ""                                       # 绝不无限等待
        raw = p.stdout or b""
        for enc in ("utf-8", "gbk", "utf-16", "latin-1"):
            try:
                return raw.decode(enc)
            except Exception:                               # noqa: BLE001
                continue
        return raw.decode("utf-8", "ignore")


def extract_pasted(text: str, kind: str) -> tuple[str, list[str], dict]:
    """剪贴板 / 标准输入等纯文本来源。"""
    title = ""
    m = re.search(r"^\s*#\s+(.+)$", text, re.M)
    if m:
        title = m.group(1).strip()
    if not title:
        for line in text.splitlines():
            line = line.strip()
            if len(line) >= 4:
                title = line[:40]
                break
    body = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.M)
    body = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", body)
    body = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", body)
    paras = split_paragraphs(body)
    if len(paras) > 1 and title and paras[0].strip() == title.strip():
        paras = paras[1:]
    info = {"source": kind, "source_type": kind, "author": "", "date": "", "sitename": ""}
    return title or "剪贴板文本", paras, info


def extract_url(url: str) -> tuple[str, list[str], dict]:
    """网页 → (标题, 段落列表, 元信息)。优先 trafilatura，失败降级 urllib。"""
    import trafilatura

    raw = trafilatura.fetch_url(url)
    if not raw:
        import urllib.request

        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
        except Exception as e:
            raise SystemExit(
                f"\n× 网页抓取失败：{type(e).__name__}: {e}\n"
                f"  该站点可能需要登录、被反爬拦截，或当前网络不可达。改用以下任一方式：\n"
                f"    1) 浏览器里 Ctrl+A 全选正文 → Ctrl+C 复制 → python readaloud.py clipboard\n"
                f"    2) 浏览器 Ctrl+P 打印成 PDF（或另存 .html）→ python readaloud.py 路径\n"
                f"    3) 挂代理后重试：设置环境变量 HTTP(S)_PROXY"
            )
    if not raw:
        raise SystemExit("× 页面内容为空（可能是纯 JS 渲染）。请改用 clipboard 方式。")

    meta = trafilatura.extract(raw, with_metadata=True, output_format="json")
    meta_obj = json.loads(meta) if isinstance(meta, str) else (meta or {})
    body = trafilatura.extract(
        raw,
        include_comments=False,
        include_tables=True,
        no_fallback=False,
        favor_precision=True,
    )
    if not body or len(body.strip()) < 60:
        body = trafilatura.extract(raw, favor_recall=True) or ""
    title = (meta_obj.get("title") or "").strip() or url
    paras = split_paragraphs(body)
    info = {
        "source": url,
        "source_type": "网页",
        "author": (meta_obj.get("author") or "").strip(),
        "date": (meta_obj.get("date") or "").strip(),
        "sitename": (meta_obj.get("sitename") or "").strip(),
    }
    return title, paras, info


def extract_pdf(path: Path, pages=None) -> tuple[str, list[str], dict]:
    """PDF → (标题, 段落列表, 元信息)。

    pages 可为 None、(起,止) 或 [(起,止), ...]（多段合并成一篇）。
    """
    try:
        import pymupdf as fitz            # PyMuPDF >= 1.24 的新包名
    except ImportError:
        import fitz                       # 兼容旧版本

    doc = fitz.open(str(path))
    meta_title = (doc.metadata or {}).get("title") or ""
    total_pages = doc.page_count
    if not pages:
        ranges = [(1, total_pages)]
    elif isinstance(pages, tuple) and len(pages) == 2 and isinstance(pages[0], int):
        ranges = [pages]
    else:
        ranges = list(pages)
    ranges = merge_ranges([(max(1, a), min(total_pages, b)) for a, b in ranges])
    if not ranges or ranges[0][0] > ranges[-1][1]:
        doc.close()
        raise SystemExit(f"× 页范围 {ranges} 超出文档页数（共 {total_pages} 页）")
    wanted = [q for a, b in ranges for q in range(a - 1, b)]     # 0-based 页号
    npage = len(wanted)
    spec_txt = "、".join(f"{a}-{b}" if a != b else f"{a}" for a, b in ranges)

    # 第一遍：按页收集文本块（含字号），纵向位置归一化为页面高度比例
    raw_pages: list[list[dict]] = []
    for pno in wanted:
        page = doc.load_page(pno)
        h = page.rect.height or 1.0
        items = []
        for b in page.get_text("dict").get("blocks", []):
            if b.get("type", 0) != 0:                     # 跳过图片块
                continue
            x0, y0, _, y1 = b["bbox"]
            lines, size = [], 0.0
            for ln in b.get("lines", []):
                spans = ln.get("spans", [])
                t = "".join(s.get("text", "") for s in spans).strip()
                if not t:
                    continue
                lines.append(t)
                for s in spans:
                    size = max(size, float(s.get("size", 0.0) or 0.0))
            if not lines:
                continue
            items.append({"x0": x0, "rt": y0 / h, "rb": y1 / h,
                          "text": "\n".join(lines), "size": size})
        raw_pages.append(items)

    from collections import Counter
    import statistics

    def sig(t: str) -> str:
        return re.sub(r"\d+", "#", re.sub(r"\s+", "", t))

    def near_edge(it: dict) -> bool:
        return it["rt"] < 0.12 or it["rb"] > 0.88

    # 目录页识别：一页里出现多处「标题 . . . . 页码」引导线 → 整页跳过
    toc_pages = {i for i, items in enumerate(raw_pages)
                 if sum(1 for it in items if TOC_LEADER.search(it["text"])) >= 3}

    # 第二遍：页眉页脚 = 上下边缘短行，且「抹掉数字后」跨页重复
    edge_cnt: Counter = Counter()
    for i, items in enumerate(raw_pages):
        if i in toc_pages:
            continue
        for it in items:
            if len(it["text"]) < 60 and near_edge(it):
                edge_cnt[sig(it["text"])] += 1
    junk = {k for k, v in edge_cnt.items() if v >= 2} if npage >= 2 else set()

    blocks: list[dict] = []
    for i, items in enumerate(raw_pages):
        if i in toc_pages:                                 # 目录页整页不朗读
            continue
        for it in items:
            flat = re.sub(r"\s*\n\s*", "", it["text"]).strip()
            if not flat or is_noise(flat) or AXIS_NUMS.match(flat):
                continue
            if len(flat) < 60 and near_edge(it) and sig(flat) in junk:
                continue
            blocks.append({**it, "text": flat, "pno": wanted[i] + 1})
    doc.close()
    blocks.sort(key=lambda b: (b["pno"], b["rt"], b["x0"]))

    # 正文字号 = 较长文本块字号的中位数；明显更大者判为标题（强制断段）
    body_sizes = [b["size"] for b in blocks if len(b["text"]) >= 25]
    med = statistics.median(body_sizes) if body_sizes else 0.0
    hard_end = set(SENT_END + SENT_KEEP_AFTER + "：:")
    list_head = re.compile(r"^\s*(?:[（(]?\d+[、.．)）]|[一二三四五六七八九十]+[、.]|[-*•·])")

    paras: list[str] = []
    prev: dict | None = None
    for b in blocks:
        t = b["text"]
        is_head = bool(med) and b["size"] >= med * 1.18
        if is_head:                                       # 标题独立成段
            paras.append(t)
            prev = {**b, "heading": True}
            continue
        same_flow = prev is not None and not prev.get("heading") \
            and abs(b["size"] - prev["size"]) < 0.6
        # 同字号、上一段未以句末标点收尾 → 是被换行截断的同一句，粘连
        if paras and same_flow and not CODEISH.search(t) and not list_head.match(t) \
                and paras[-1][-1] not in hard_end:
            paras[-1] += t
        else:
            paras.append(t)
        prev = b

    final: list[str] = []
    for p in paras:
        final.extend(hard_split_paragraph(p) if len(p) > 900 else [p])
    paras = [p for p in final if p and not is_noise(p)]
    title = meta_title.strip() or (paras[0][:40] if paras else path.stem)
    if paras and paras[0].strip() == title.strip() and len(paras) > 1:
        paras = paras[1:]
    info = {
        "source": str(path),
        "source_type": "PDF",
        "author": "",
        "date": "",
        "sitename": (f"第 {spec_txt} 页 / 共 {total_pages} 页" if pages
                     else f"共 {total_pages} 页"),
    }
    return title, paras, info


def extract_text(path: Path) -> tuple[str, list[str], dict]:
    """txt / md → (标题, 段落列表, 元信息)。"""
    raw = path.read_text(encoding="utf-8", errors="ignore")
    title = path.stem
    # 优先一级标题，其次任意级别首个标题，最后退回文件名
    m = re.search(r"^\s*#\s+(.+)$", raw, re.M) or re.search(r"^\s*#{1,6}\s+(.+)$", raw, re.M)
    if m:
        title = m.group(1).strip()
    body = re.sub(r"^\s*#{1,6}\s*", "", raw, flags=re.M)      # 去标题井号
    body = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", body)          # 去图片
    body = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", body)      # 链接留文字
    body = re.sub(r"^\s*>\s?", "", body, flags=re.M)          # 去引用符
    body = re.sub(r"^\s*[-*+]\s+", "", body, flags=re.M)      # 去列表符
    body = re.sub(r"[*_`~#|>-]{2,}", " ", body)               # 去装饰符号串
    paras = split_paragraphs(body)
    # 首段与标题相同时才去掉，且必须还有后续段落（否则单行内容会被吃空）
    if len(paras) > 1 and paras[0].strip() == title.strip():
        paras = paras[1:]
    info = {"source": str(path), "source_type": "文本", "author": "", "date": "", "sitename": ""}
    return title, paras, info


def split_paragraphs(text: str, soft_merge: bool = False) -> list[str]:
    """按空行/换行切段，清洗噪声，合并过短碎片。

    soft_merge=True 用于 PDF：部分 PDF 每行是一个独立 block，
    若上一段以非句末标点收尾，则与下一行粘连，避免句子被腰斩。
    """
    text = text.replace("\r\n", "\n").replace("\x0c", "\n\n")
    text = re.sub(r"[ \t\u3000]+", " ", text)
    chunks = [c.strip() for c in re.split(r"\n\s*\n|\n", text) if c and c.strip()]
    hard_end = set(SENT_END + SENT_KEEP_AFTER + "：:")
    list_head = re.compile(r"^\s*(?:[（(]?\d+[、.．)）]|[一二三四五六七八九十]+[、.]|[-*•·])")
    out: list[str] = []
    for c in chunks:
        c = c.replace("\n", "").strip()
        if not c or is_noise(c):
            continue
        if len(c) < 8 and out:                        # 过短碎片并入上一段
            out[-1] += c
            continue
        if soft_merge and out and len(c) < 45 and not list_head.match(c) \
                and out[-1][-1] not in hard_end:      # 软换行粘连
            out[-1] += c
            continue
        out.append(c)
    # 超长段落（PDF 常见整页粘连）按句数再切
    final: list[str] = []
    for p in out:
        if len(p) <= 900:
            final.append(p)
        else:
            final.extend(hard_split_paragraph(p))
    return final


def hard_split_paragraph(p: str) -> list[str]:
    sents = split_sentences(p)
    groups, buf, n = [], [], 0
    for s in sents:
        buf.append(s)
        n += len(s)
        if n >= 500:
            groups.append("".join(buf))
            buf, n = [], 0
    if buf:
        groups.append("".join(buf))
    return groups


def is_noise_legacy(text: str) -> bool:  # pragma: no cover - 保留占位，避免重复定义
    return False


# ---------------------------------------------------------------- 2. 句子切分

def split_sentences(paragraph: str) -> list[str]:
    """中英混排断句，保留引号/括号收尾。"""
    sents: list[str] = []
    buf: list[str] = []
    for ch in paragraph:
        buf.append(ch)
        if ch in SENT_END:
            # 吸收紧随其后的收尾引号
            while True:
                sents.append("".join(buf))
                buf = []
                break
            continue
    if buf:
        sents.append("".join(buf))

    merged: list[str] = []
    for s in sents:
        s = s.strip()
        if not s:
            continue
        if merged and len(merged[-1]) < 4:
            merged[-1] += s
        else:
            merged.append(s)

    # 处理无标点超长句 / 连续多个终止符后的引号粘连
    out: list[str] = []
    for s in merged:
        if len(s) <= MAX_SENTENCE_CHARS:
            out.append(s)
        else:
            out.extend(break_long(s))
    # 把行尾孤立收尾符粘回前句
    fixed: list[str] = []
    for s in out:
        if fixed and s.strip() and all(c in SENT_KEEP_AFTER for c in s.strip()):
            fixed[-1] += s
        else:
            fixed.append(s)
    return fixed


def break_long(s: str) -> list[str]:
    parts, buf, n = [], [], 0
    for ch in s:
        buf.append(ch)
        n += 1
        if n >= MAX_SENTENCE_CHARS and ch in CN_SPLIT:
            parts.append("".join(buf).strip())
            buf, n = [], 0
    if buf:
        parts.append("".join(buf).strip())
    return [p for p in parts if p]


def normalize_len(s: str) -> int:
    return len(NORMALIZE_KEEP.findall(s))


# ---------------------------------------------------------------- 3. 语音合成

def build_chunks(paragraphs) -> list[list[int]]:
    """把句子打包成 TTS 请求块，保证句子不跨块。返回 [[para_idx, sent_idx], ...]"""
    chunks: list[list[int]] = []
    cur: list[int] = []
    size = 0
    for pi, sents in enumerate(paragraphs):
        for si, s in enumerate(sents):
            item = [pi, si]
            add = len(s[0]) + 1
            if cur and size + add > MAX_CHUNK_CHARS:
                chunks.append(cur)
                cur, size = [], 0
            cur.append(item)
            size += add
    if cur:
        chunks.append(cur)
    return chunks


class Cancelled(Exception):
    """用户中途取消合成。"""


def _cancelled(fn) -> bool:
    try:
        return bool(fn and fn())
    except Exception:                                       # noqa: BLE001
        return False


async def synth_chunk(text: str, out_path: Path, voice: str, rate: str, pitch: str,
                      should_cancel=None):
    """合成单块音频，返回 (mp3 bytes, 句边界事件列表)。

    用 SentenceBoundary：服务端直接返回整句 offset/duration（单位 100ns），
    实测与本地断句高度一致，比词级时间戳更准更省。
    should_cancel 每收到一个数据帧就查一次，取消延迟通常不到 1 秒。
    """
    import edge_tts

    comm = edge_tts.Communicate(text, voice=voice, rate=rate, pitch=pitch,
                                boundary="SentenceBoundary")
    audio = bytearray()
    cues: list[dict] = []
    agen = comm.stream()
    try:
        async for msg in agen:
            if _cancelled(should_cancel):
                raise Cancelled("已取消")
            if msg["type"] == "audio":
                audio.extend(msg["data"])
            elif msg["type"] in ("SentenceBoundary", "WordBoundary"):
                cues.append({"text": msg.get("text", ""), "offset": msg["offset"],
                             "duration": msg["duration"], "kind": msg["type"]})
    finally:
        try:
            await agen.aclose()               # 断掉底层 aiohttp 请求，别留悬挂连接
        except Exception:                     # noqa: BLE001
            pass
    if not audio:
        raise RuntimeError("TTS 返回空音频")
    out_path.write_bytes(bytes(audio))
    return bytes(audio), cues


def mp3_length(path: Path) -> float:
    try:
        from mutagen.mp3 import MP3

        return float(MP3(str(path)).info.length)
    except Exception:
        return 0.0


def map_cues_to_sentences(sent_texts: list[str], cues: list[dict],
                          base_ms: float, chunk_dur_ms: float) -> list[float]:
    """把句边界事件对齐回本地句子，返回每句起始毫秒（单调不减）。"""
    n = len(sent_texts)
    if n == 0:
        return []
    lens = [max(1, normalize_len(s)) for s in sent_texts]
    total = sum(lens)

    # 快路径：服务端分句与本地 1:1
    if len(cues) == n:
        return [base_ms + c["offset"] / 1e4 for c in cues]

    # 通用路径：把 cue 当作「字符位置 → 绝对时间」的锚点，做分段线性插值
    anchors: list[tuple[int, float]] = []
    pos = 0
    for c in cues:
        anchors.append((pos, base_ms + c["offset"] / 1e4))
        pos += max(1, normalize_len(c["text"]))
    rate_ms = chunk_dur_ms / total if total else 0.0

    def char_to_ms(p: int) -> float:
        if not anchors:
            return base_ms + p * rate_ms
        if p <= anchors[0][0]:
            return anchors[0][1]
        for k in range(len(anchors) - 1):
            p0, t0 = anchors[k]
            p1, t1 = anchors[k + 1]
            if p0 <= p < p1:
                span = max(1, p1 - p0)
                return t0 + (p - p0) / span * (t1 - t0)
        pl, tl = anchors[-1]
        return tl + (p - pl) * rate_ms

    out, acc = [], 0
    for L in lens:
        out.append(char_to_ms(acc))
        acc += L
    # 强制单调
    for i in range(1, n):
        out[i] = max(out[i], out[i - 1])
    return out


async def synthesize(paragraphs: list[list[str]], workdir: Path, voice: str,
                     rate: str, pitch: str, should_cancel=None) -> tuple[Path, float]:
    """逐块合成 → 拼接 mp3 → 回填句级时间轴。

    音频先写 article.part.mp3，全部成功后才改名成 article.mp3，
    所以中途取消不会破坏该作品已有的完整版本。
    """
    import shutil

    tmp = workdir / "_tts"
    tmp.mkdir(exist_ok=True)
    chunk_plan = build_chunks(paragraphs)
    part = workdir / "article.part.mp3"
    final = workdir / "article.mp3"
    base_ms = 0.0
    total_chars = sum(len(s[0]) for ps in paragraphs for s in ps)
    t0 = time.time()

    try:
        part.write_bytes(b"")
        for ci, plan in enumerate(chunk_plan, 1):
            if _cancelled(should_cancel):
                raise Cancelled("已取消")
            sents = [paragraphs[pi][si][0] for pi, si in plan]
            text = "\n".join(sents)
            fp = tmp / f"c{ci:04d}.mp3"
            for attempt in range(3):
                try:
                    audio, cues = await synth_chunk(text, fp, voice, rate, pitch,
                                                    should_cancel)
                    break
                except Cancelled:
                    raise                       # 取消不是网络错误，绝不重试
                except Exception as e:  # 网络抖动重试
                    if attempt == 2:
                        raise
                    log(f"  ! 第 {ci} 块失败({e})，重试 {attempt + 1}/2")
                    await asyncio.sleep(1.5 * (attempt + 1))
            dur = mp3_length(fp)
            dur_ms = dur * 1000 if dur else sum(
                normalize_len(s) * 45 for s in sents)  # 兜底估算
            starts = map_cues_to_sentences(sents, cues, base_ms, dur_ms)
            for (pi, si), st in zip(plan, starts):
                paragraphs[pi][si][1] = st          # 回填 start_ms
            with part.open("ab") as f:
                f.write(audio)
            base_ms += dur_ms
            done = sum(len(s) for s in sents)
            log(f"  [{ci}/{len(chunk_plan)}] {done}字 / 累计{base_ms / 1000:.1f}s "
                f"({time.time() - t0:.0f}s 用时, {100 * done / max(1, total_chars):.0f}%)")
        part.replace(final)
        return final, base_ms / 1000
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            part.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- 4. 产物

def slugify(title: str, src: str, variant: str = "") -> str:
    """同一份内容+同一页范围 = 同一个作品目录。

    variant 必须参与哈希，否则「第1章」和「整本书」会落到同一目录互相覆盖。
    """
    keep = re.sub(r"[^\w\u4e00-\u9fff-]+", "", title)[:28].strip("-") or "article"
    h = hashlib.md5(f"{src}|{variant}".encode("utf-8")).hexdigest()[:6]
    return f"{keep}_{h}"


def render_html(data: dict, audio_name: str) -> str:
    tpl = TEMPLATE_PATH.read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return tpl.replace("__AUDIO_SRC__", html.escape(audio_name, quote=True)) \
              .replace("__DATA_JSON__", payload)


def build_payload(title: str, structured: list, info: dict, voice: str,
                  rate: str, dur_s: float, total: int) -> dict:
    """组装给播放器的数据。命令行与网页版共用，避免两边逻辑分叉。"""
    return {
        "title": title,
        "info": info,
        "voice": voice,
        "rate": rate,
        "words": total,
        "duration": round(dur_s, 1),
        "created": time.strftime("%Y-%m-%d %H:%M"),
        "paragraphs": [
            {"sentences": [{"text": s[0], "start": round(s[1], 1)} for s in ps]}
            for ps in structured
        ],
    }


# ---------------------------------------------------------------- 5. 主流程

CANDIDATE_ROOTS = (
    "Downloads",
    "Documents",
    "Desktop",
)


def where(p: Path) -> str:
    """把文件所在目录压成简短形式，用于区分同名文件。"""
    try:
        home = str(Path.home())
        s = str(p.parent)
        if s.lower().startswith(home.lower()):
            s = "~" + s[len(home):]
        parts = [x for x in s.replace("\\", "/").split("/") if x]
        tail = parts[-2:] if len(parts) > 2 else parts
        pre = "…\\" if len(parts) > 2 else ""
        return pre + "\\".join(tail)
    except Exception:
        return str(p.parent)


def pick_interactive() -> str:
    """不带参数运行时：扫描常见目录，列出可选文件让用户敲编号。"""
    exts = {".pdf", ".txt", ".md", ".markdown", ".html", ".htm"}
    roots = [Path.home() / d for d in CANDIDATE_ROOTS]
    roots.append(SCRIPT_DIR / "samples")

    files: list[Path] = []
    seen = set()
    for r in roots:
        if not r.is_dir():
            continue
        cand: list[Path] = []
        try:
            cand = list(r.glob("*"))
            for d in r.glob("*"):
                if d.is_dir() and not d.name.startswith((".", "$")):
                    cand.extend(d.glob("*"))
        except OSError:
            continue
        for p in cand:
            try:
                if p.is_file() and p.suffix.lower() in exts and str(p) not in seen:
                    seen.add(str(p))
                    files.append(p)
            except OSError:
                continue
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    files = files[:15]

    log("")
    log("  最近的文件（输入编号即可朗读）：")
    if files:
        for i, p in enumerate(files, 1):
            age = time.strftime("%m-%d", time.localtime(p.stat().st_mtime))
            log(f"   {i:>2}. [{age}] {p.name}")
            log(f"{'':>13}⌐ {where(p)}")
    else:
        log("    （下载/文档目录里没找到 pdf/txt/md 文件）")
    log("")
    log("  也可以直接输入：网页地址 / 文件完整路径 / c=读剪贴板 / q=退出")
    try:
        raw = input("  > ").strip().strip('"').strip("'")
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("\n已退出")
    if not raw:
        raise SystemExit("× 没有输入内容")
    if raw.lower() in ("q", "quit", "exit"):
        raise SystemExit("已退出")
    if raw.lower() in ("c", "clipboard", "剪贴板"):
        return "clipboard"
    if raw.isdigit():
        idx = int(raw) - 1
        if not 0 <= idx < len(files):
            raise SystemExit(f"× 编号超出范围（1~{len(files)}）")
        return str(files[idx])
    return raw


# 像代码的块不参与段落粘连（代码行末通常没有标点）
CODEISH = re.compile(r"[{};=<>|]{2,}|->|=>|^\s*(?:def |class |import |from |return |if |for |while |\})")


def parse_pages(spec: str) -> list[tuple[int, int]] | None:
    """解析页范围，支持多段：`20-35`、`5`、`9-14,38-79`、`1-3 6-8`。"""
    spec = (spec or "").strip()
    if not spec:
        return None
    out: list[tuple[int, int]] = []
    for part in re.split(r"[,，;；\s]+", spec):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*[-–~]\s*(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            out.append((min(a, b), max(a, b)))
        elif part.isdigit():
            out.append((int(part), int(part)))
        else:
            raise SystemExit(f"× 页范围格式应为 3-20 或 5 或 3-5,8-10，收到：{spec}")
    out.sort()
    return out or None


def merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """把重叠/相邻的页区间合并，避免多段填写时重复朗读。"""
    if not ranges:
        return []
    res = [ranges[0]]
    for a, b in ranges[1:]:
        pa, pb = res[-1]
        if a <= pb + 1:
            res[-1] = (pa, max(pb, b))
        else:
            res.append((a, b))
    return res


def load_source(arg: str, pages: tuple[int, int] | None = None) -> tuple[str, list[str], dict]:
    arg = (arg or "").strip().strip('"').strip("'")
    if not arg:
        raise SystemExit("× 没有指定内容。直接运行本脚本可选择文件，或参考 README.md。")
    if arg == "-":
        return extract_pasted(sys.stdin.read(), "标准输入")
    if arg.lower() in ("clipboard", "clip", "剪贴板"):
        t = read_clipboard()
        if not t.strip():
            raise SystemExit("× 剪贴板为空。请先在浏览器里 Ctrl+A 全选正文、Ctrl+C 复制。")
        if len(t.strip()) < 30:
            raise SystemExit(
                f"× 剪贴板只有 {len(t.strip())} 个字：「{t.strip()[:30]}」\n"
                f"  看起来复制的是文件名或路径，不是正文。\n"
                f"  请在文章页面里 Ctrl+A 全选正文 → Ctrl+C → 再试一次。")
        return extract_pasted(t, "剪贴板")
    if re.match(r"^https?://", arg, re.I):
        return extract_url(arg)
    p = Path(arg).expanduser()
    if p.is_dir():
        raise SystemExit(
            f"× 这是文件夹不是文件：{p.resolve()}\n"
            f"  请指定具体文件（例如 {p / 'xxx.pdf'}），或运行不带参数用编号选择。")
    p = p.resolve()
    if not p.exists():
        raise SystemExit(f"× 文件不存在：{p}")
    ext = p.suffix.lower()
    if ext == ".pdf":
        return extract_pdf(p, pages)
    if ext in (".doc", ".docx", ".wps"):
        raise SystemExit(
            f"× 暂不支持 {ext}。请在 Word 里「另存为 PDF」后再朗读。")
    if ext in (".txt", ".md", ".markdown", ".rst", ".csv", ""):
        return extract_text(p)
    if ext in (".html", ".htm"):
        import trafilatura

        raw = p.read_bytes()
        body = trafilatura.extract(raw) or ""
        return p.stem, split_paragraphs(body), {
            "source": str(p), "source_type": "本地HTML",
            "author": "", "date": "", "sitename": ""}
    raise SystemExit(f"× 暂不支持的格式: {ext}（支持 pdf / txt / md / html / URL）")


async def cmd_list_voices() -> None:
    import edge_tts

    voices = await edge_tts.list_voices()
    cn = [v for v in voices if str(v.get("Locale", "")).lower().startswith("zh-")]
    log(f"{'音色名':<36} {'性别':<8} Locale")
    for v in sorted(cn, key=lambda x: x.get("ShortName", "")):
        log(f"{v.get('ShortName',''):<36} {v.get('Gender',''):<8} {v.get('Locale','')}")


async def amain() -> int:
    ap = argparse.ArgumentParser(
        description="网页/PDF/文本 → 同步高亮有声读物", add_help=True)
    ap.add_argument("target", nargs="?", default="",
                    help="URL 或文件路径；list=列出中文音色；不填则进入选择菜单")
    ap.add_argument("-o", "--outdir", default=str(DEFAULT_OUTDIR))
    ap.add_argument("--voice", default=DEFAULT_VOICE)
    ap.add_argument("--rate", default="+0%", help="语速，如 +15%% / -20%%")
    ap.add_argument("--pitch", default="+0Hz")
    ap.add_argument("--max-chars", type=int, default=-1,
                    help="截断字数；不填时超大文档自动限 20000 字，0=不限")
    ap.add_argument("--pages", default="", help="PDF 页范围，如 3-20 或 5（默认全部）")
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--serve", action="store_true", help="起本地 HTTP 服务打开")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    if args.target.strip().lower() == "list":
        await cmd_list_voices()
        return 0

    if args.target.strip().lower() == "toc":
        rest = (args.pages or "").strip() or pick_interactive()
        p = Path(rest).expanduser()
        if not p.is_file():
            raise SystemExit(f"× 文件不存在：{p}")
        if p.suffix.lower() != ".pdf":
            raise SystemExit(f"× 只有 PDF 有页码概念，{p.suffix} 不用填页范围")
        import booktoc

        print(booktoc.format_table(booktoc.detect(p)))
        return 0

    target = args.target.strip() or pick_interactive()
    pages = parse_pages(args.pages)
    if pages:
        log(f"① 解析内容：{target}（第 "
            + "、".join(f"{a}-{b}" if a != b else f"{a}" for a, b in pages)
            + " 页）")
    else:
        log(f"① 解析内容：{target}")
    title, paras, info = load_source(target, pages)
    if not paras:
        log("× 没有抽到正文。可能是扫描版 PDF（整页是图片，需先 OCR）或纯 JS 渲染页面。")
        log("  扫描件：用 WPS / Adobe 的「文字识别」转一次再朗读；网页：改用 clipboard 方式。")
        return 2
    total0 = sum(len(p) for p in paras)
    limit = args.max_chars
    if limit < 0 and total0 > SOFT_LIMIT:
        limit = SOFT_LIMIT
        log(f"   ! 文档较大（{total0} 字 ≈ {total0 / 400:.0f} 分钟音频），已自动只取前 {SOFT_LIMIT} 字")
        log(f"     只读某一章 → 加 --pages 20-35      坚持读完整本 → 加 --max-chars 0")
    if limit > 0:
        acc, cut = 0, []
        for p in paras:
            acc += len(p)
            cut.append(p)
            if acc >= limit:
                break
        paras = cut
    total = sum(len(p) for p in paras)
    log(f"   标题：{title}")
    log(f"   段落 {len(paras)} 个 / 正文 {total} 字 / 来源 {info['source_type']}")

    # 句子化，并预置时间槽
    structured = [[list([s, 0.0]) for s in split_sentences(p)] for p in paras]

    outroot = Path(args.outdir).expanduser().resolve()
    workdir = outroot / slugify(title, info["source"], args.pages or "")
    workdir.mkdir(parents=True, exist_ok=True)

    log(f"② 合成语音（{args.voice}，语速 {args.rate}）")
    t0 = time.time()
    audio, dur_s = await synthesize(structured, workdir, args.voice, args.rate, args.pitch)
    log(f"   音频 {dur_s:.0f}s，合成耗时 {time.time() - t0:.0f}s")

    data = build_payload(title, structured, info, args.voice, args.rate, dur_s, total)
    (workdir / "article.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    page = workdir / "index.html"
    page.write_text(render_html(data, audio.name), encoding="utf-8")
    log(f"③ 完成 → {page}")

    if args.serve:
        import functools
        import http.server
        import socketserver

        handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                    directory=str(workdir))
        socketserver.TCPServer.allow_reuse_address = True
        with socketserver.TCPServer(("127.0.0.1", args.port), handler) as httpd:
            url = f"http://127.0.0.1:{args.port}/index.html"
            log(f"④ 本地服务 {url}（Ctrl+C 结束）")
            if not args.no_open:
                webbrowser.open(url)
            httpd.serve_forever()
    elif not args.no_open:
        webbrowser.open(page.as_uri())
    return 0


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        raise SystemExit(asyncio.run(amain()))
    except KeyboardInterrupt:
        log("\n已取消")
        raise SystemExit(130)


if __name__ == "__main__":
    main()
