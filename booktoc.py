#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""booktoc.py —— 从 PDF 里认出「章节 → 页范围」。

用户不知道页码，所以要让 PDF 自己说。按可靠性三级兜底：

    1. bookmark  PDF 内置书签（最准，正规电子书/官方文档通常有）
    2. tocpage   正文前的目录页（「第3章 xxx 72」这种），再回正文定位真实页
    3. fontsize  字号启发：明显大于正文、且形如「第N章/引言/附录」的标题行

都识别不出来时返回 method="none"，由调用方给出「按页数均分」的建议。
"""

from __future__ import annotations

import re
import statistics
from pathlib import Path

# 章级标题特征
CHAP_PAT = re.compile(
    r"^(?:第\s*[0-9零一二三四五六七八九十百千]+\s*[章节篇部讲]"
    r"|引言|前言|序言|序章|导论|绪论|附录|后记|结语|总结|参考文献|术语表|索引"
    r"|致谢|目录|版本说明|Chapter\s+\d+|Part\s+\d+)", re.I)
SUBSEC = re.compile(r"^\d+\.\d+")           # 1.1 这种小节，不算章
WORD_PER_MIN = 320.0                        # 实测中文神经语音约 320 字/分钟


def _clean(t: str) -> str:
    return re.sub(r"\s+", " ", t or "").strip()


def _key(t: str) -> str:
    return re.sub(r"[\s\.。，,、]+", "", t)[:18].lower()


def _is_chapter(t: str) -> bool:
    t2 = _clean(t).replace(" ", "")
    return bool(t2) and not SUBSEC.match(t2) and bool(CHAP_PAT.match(t2))


def _chars(doc, a: int, b: int) -> int:
    """[a,b] 为 1-based 闭区间的字数（去掉空白）。"""
    return sum(len(re.sub(r"\s+", "", doc.load_page(q).get_text() or ""))
               for q in range(max(1, a) - 1, min(doc.page_count, b)))


def _bounds(items: list[tuple[str, int]], n: int) -> list[dict]:
    """把 (标题, 起始页) 变成带结束页/字数/时长的章节表，并剔除非法与重叠。"""
    pts = sorted({(t, max(1, min(n, p))) for t, p in items}, key=lambda x: x[1])
    keep: list[tuple[str, int]] = []
    for t, p in pts:
        if keep and p <= keep[-1][1]:        # 页码没往前走 → 同一章的重复书签
            continue
        keep.append((t, p))
    out = []
    for i, (t, p) in enumerate(keep):
        end = (keep[i + 1][1] - 1) if i + 1 < len(keep) else n
        if end < p:
            continue
        out.append({"title": _clean(t), "from": p, "to": end})
    return out


# ---------------------------------------------------------------- 策略 1：书签

def _from_bookmark(doc, n: int) -> list[dict]:
    try:
        toc = doc.get_toc(simple=True)
    except Exception:                                        # noqa: BLE001
        return []
    if not toc:
        return []
    for level in (1, 2, 3):
        picks = [(_clean(t), int(p)) for lv, t, p in toc if lv == level and _clean(t)]
        if not picks:
            continue
        chapters = [x for x in picks if _is_chapter(x[0])]
        use = chapters if len(chapters) >= 2 else picks
        if len(use) >= 2:
            return _bounds(use, n)
    return []


# ------------------------------------------------------------- 策略 2：目录页

_ENTRY = re.compile(r"^(.{2,60}?)\s*[.·⋯…\s]{0,}(\d{1,4})\s*$")


def _from_tocpage(doc, n: int) -> list[dict]:
    """先找目录页，解析「标题 + 印刷页码」，再回正文定位真实 PDF 页。"""
    entries: list[tuple[str, int]] = []
    toc_pages = 0
    for i in range(min(n, 14)):                  # 目录一般在最前面十几页
        found = 0
        for b in doc.load_page(i).get_text("dict").get("blocks", []):
            if b.get("type", 0):
                continue
            t = _clean(" ".join("".join(s.get("text", "") for s in ln.get("spans", []))
                                for ln in b.get("lines", [])))
            if not t or len(t) > 60:
                continue
            m = _ENTRY.match(t)
            if m and _is_chapter(m.group(1)):
                entries.append((_clean(m.group(1)), int(m.group(2))))
                found += 1
        if found >= 3:
            toc_pages += 1
    if len(entries) < 2 or toc_pages == 0:
        return []

    # 同名条目只留第一个（目录里"思考题"等会重复出现）
    seen, uniq = set(), []
    for t, p in entries:
        k = _key(t)
        if k in seen:
            continue
        seen.add(k)
        uniq.append((t, p))

    located: list[tuple[str, int]] = []
    for t, printed in uniq:
        real = _locate(doc, n, t)
        if real:
            located.append((t, real))
    if len(located) < 2:
        # 定位不到就用「印刷页码 + 固定偏移」推算（偏移取首个能定位到的章）
        offs = []
        for t, printed in uniq:
            r = _locate(doc, n, t)
            if r:
                offs.append(r - printed)
        if not offs:
            return []
        off = statistics.mode(offs)
        located = [(t, max(1, p + off)) for t, p in uniq]
    return _bounds(located, n)


def _locate(doc, n: int, title: str) -> int | None:
    """在正文里找该章标题真实所在的页（跳过目录页）。"""
    key = _key(title)
    if not key:
        return None
    for i in range(n):
        if i < 2:
            continue
        for b in doc.load_page(i).get_text("dict").get("blocks", []):
            if b.get("type", 0):
                continue
            txt = _clean(" ".join("".join(s.get("text", "") for s in ln.get("spans", []))
                                  for ln in b.get("lines", [])))
            if len(txt) > 50:
                continue
            k2 = _key(txt)
            if not k2:
                continue
            if k2 == key or k2.startswith(key[:8]) or key.startswith(k2[:8]):
                return i + 1
    return None


# ------------------------------------------------------------- 策略 3：字号

def _from_fontsize(doc, n: int) -> list[dict]:
    sizes: list[float] = []
    blocks: list[list[tuple[str, float]]] = []
    for i in range(n):
        items = []
        for b in doc.load_page(i).get_text("dict").get("blocks", []):
            if b.get("type", 0):
                continue
            y0, y1 = b["bbox"][1], b["bbox"][3]
            h = doc.load_page(i).rect.height or 1
            if y0 / h < 0.10 or y1 / h > 0.90:               # 跳过页眉页脚
                continue
            t = _clean(" ".join("".join(s.get("text", "") for s in ln.get("spans", []))
                                for ln in b.get("lines", [])))
            if not t:
                continue
            sz = max((float(s.get("size", 0) or 0) for ln in b.get("lines", [])
                      for s in ln.get("spans", [])), default=0.0)
            if len(t) >= 25:
                sizes.append(sz)
            items.append((t, sz))
        blocks.append(items)
    if not sizes:
        return []
    med = statistics.median(sizes)
    if med <= 0:
        return []
    picks: list[tuple[str, int]] = []
    for i, items in enumerate(blocks):
        for t, sz in items:
            if sz >= med * 1.4 and len(t) <= 40 and _is_chapter(t):
                picks.append((t, i + 1))
                break
    return _bounds(picks, n) if len(picks) >= 2 else []


# ---------------------------------------------------------------- 对外接口

def detect(path: Path) -> dict:
    """返回 {"total_pages", "method", "items":[{title,from,to,chars,audio_min}], "note"}"""
    try:
        import pymupdf as fitz
    except ImportError:                                        # pragma: no cover
        import fitz                                            # type: ignore

    out = {"total_pages": 0, "method": "none", "items": [], "note": ""}
    doc = fitz.open(str(path))
    try:
        n = doc.page_count
        out["total_pages"] = n
        if n <= 1:
            out["note"] = "只有 1 页，不用填页范围，直接开始就行。"
            return out

        for fn, name in ((_from_bookmark, "bookmark"), (_from_tocpage, "tocpage"),
                         (_from_fontsize, "fontsize")):
            try:
                items = fn(doc, n)
            except Exception:                                  # noqa: BLE001
                items = []
            if len(items) >= 2:
                out["method"] = name
                break
        else:
            out["note"] = _fallback_note(n)
            return out

        for it in items:
            it["chars"] = _chars(doc, it["from"], it["to"])
            it["audio_min"] = round(it["chars"] / WORD_PER_MIN, 1)
        out["items"] = items
        label = {"bookmark": "PDF 内置书签", "tocpage": "文前目录页",
                 "fontsize": "标题字号"}[out["method"]]
        out["note"] = f"识别到 {len(items)} 个章节（依据：{label}）。点一行就填好页范围。"
        return out
    finally:
        doc.close()


def _fallback_note(n: int) -> str:
    step = 30 if n > 120 else max(10, n // 4)
    parts = []
    a = 1
    while a <= n and len(parts) < 4:
        b = min(n, a + step - 1)
        parts.append(f"{a}-{b}")
        a = b + 1
    return (f"这个 PDF 没找到可识别的章节结构（共 {n} 页）。"
            f"可以按页数分段读，例如依次填 {'、'.join(parts)}；"
            f"或先转前面一部分，听完再往后翻。")


def format_table(res: dict) -> str:
    """给命令行用的文本表格。"""
    lines = [f"共 {res['total_pages']} 页 · {res['note']}"]
    for it in res["items"]:
        mins = it["audio_min"]
        dur = f"{mins / 60:.1f} 小时" if mins >= 60 else f"{mins:.0f} 分钟"
        lines.append(f"  {it['from']:>4}-{it['to']:<5} {it['chars']:>7}字  {dur:>8}  "
                     f"{it['title'][:38]}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                          # noqa: BLE001
        pass
    if len(sys.argv) < 2:
        print("用法：python booktoc.py 某本书.pdf")
        raise SystemExit(2)
    print(format_table(detect(Path(sys.argv[1]))))
