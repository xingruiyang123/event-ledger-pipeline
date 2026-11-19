"""原始快照存储：只追加，不改写。

用户要求（2026-11-19）
--------------------
「HTML 快照要存（存 D 盘），而且应与抽取正文分开保存。试点阶段至少保存：
  原始 HTML；最终 URL、抓取时间、HTTP 状态、编码；
  doc_id 和内容哈希；独立存放的正文抽取 JSON。
  原始 HTML 不被后续清洗覆盖；动态页面必要时另存渲染版。」

目录结构
--------
  <root>/
    raw/html/<yyyymm>/<doc_id>.html.gz     原始 HTML，只追加不改
    raw/meta/<yyyymm>/<doc_id>.json        抓取元数据
    extracted/text/<yyyymm>/<doc_id>.json  正文抽取结果，可重跑覆盖
    extracted/events/<yyyymm>/*.json       事件层，可重跑覆盖
    logs/collect_<date>.log                运行日志

为什么分三层
------------
raw 层是证据：网站改版、文章删除之后，只有它还在。
extracted 层是加工产物：规则升级要能重跑，所以可以被覆盖。
混在一起的话，改一次抽取规则就得重爬全网。

doc_id 与内容哈希
-----------------
  doc_id       = sha256(url)[:16]      一篇文章的稳定标识
  content_hash = sha256(html)[:16]     内容版本，同 URL 改稿时能看出来

如果同一 URL 再次抓到不同内容，不覆盖旧的 HTML，另存为
  <doc_id>__<content_hash>.html.gz
并把新版本记进 meta 的 versions 列表。这样「历史某天的原文是什么」可查。

压缩
----
HTML 文本压缩率约 75%。不压缩一个月 6 GB，压缩后 1.5 GB。
D 盘当前剩 121.9 GB，压缩后能跑几年。
"""
import gzip
import hashlib
import io
import json
import os
import re
import sys
from datetime import datetime, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


def _sha(s, n=16):
    if isinstance(s, str):
        s = s.encode("utf-8", "replace")
    return hashlib.sha256(s).hexdigest()[:n]


def doc_id_of(url):
    """一篇文章的稳定标识，只由 URL 决定，与内容无关。"""
    return _sha(str(url).strip())


def content_hash_of(data):
    return _sha(data)


def _ym(ts=None):
    t = ts or datetime.now(timezone.utc)
    return t.strftime("%Y%m")


class RawStore:
    """原始层 + 抽取层的读写。"""

    def __init__(self, root, compress=True):
        self.root = os.path.abspath(root)
        self.compress = compress
        for sub in ("raw/html", "raw/meta", "extracted/text",
                    "extracted/events", "logs"):
            os.makedirs(os.path.join(self.root, sub), exist_ok=True)

    # ---------- 路径 ----------
    def _html_path(self, doc_id, ym, chash=None, render=False):
        name = doc_id if not chash else "%s__%s" % (doc_id, chash)
        if render:
            name += ".rendered"
        name += ".html.gz" if self.compress else ".html"
        sub = "raw/rendered" if render else "raw/html"
        return os.path.join(self.root, sub, ym, name)

    def _meta_path(self, doc_id, ym):
        return os.path.join(self.root, "raw/meta", ym, doc_id + ".json")

    def _text_path(self, doc_id, ym):
        return os.path.join(self.root, "extracted/text", ym, doc_id + ".json")

    # ---------- 写 ----------
    def put(self, url, html, http_status=200, encoding="", final_url="",
            fetched_at=None, extra=None, rendered=False):
        """存一篇抓到的页面。返回 (doc_id, content_hash, is_new)。

        只追加：已有同 doc_id 同 content_hash 的文件不重写。
        同 URL 不同内容时另存新版本，旧版本保留。
        """
        if isinstance(html, str):
            html = html.encode("utf-8", "replace")
        html = html or b""
        did = doc_id_of(url)
        chash = content_hash_of(html)
        ts = fetched_at or datetime.now(timezone.utc)
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:  # noqa: BLE001
                ts = datetime.now(timezone.utc)
        ym = _ym(ts)
        sub = "raw/rendered" if rendered else "raw/html"
        os.makedirs(os.path.join(self.root, sub, ym), exist_ok=True)
        os.makedirs(os.path.join(self.root, "raw/meta", ym), exist_ok=True)

        hp = self._html_path(did, ym, chash, rendered)
        is_new = not os.path.isfile(hp)
        if is_new:
            data = gzip.compress(html, 6) if self.compress else html
            tmp = hp + ".tmp"
            with io.open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, hp)

        # meta：累积版本记录
        mp = self._meta_path(did, ym)
        meta = {}
        if os.path.isfile(mp):
            try:
                meta = json.load(io.open(mp, encoding="utf-8"))
            except Exception:  # noqa: BLE001
                meta = {}
        ver = {"content_hash": chash, "fetched_at": ts.isoformat(),
               "http_status": http_status, "bytes": len(html),
               "rendered": rendered,
               "file": os.path.relpath(hp, self.root).replace("\\", "/")}
        vers = meta.get("versions") or []
        if not any(v.get("content_hash") == chash for v in vers):
            vers.append(ver)
        meta.update({
            "doc_id": did,
            "url": str(url),
            "final_url": final_url or str(url),
            "encoding": encoding,
            "content_hash": chash,
            "first_fetched_at": meta.get("first_fetched_at") or ts.isoformat(),
            "last_fetched_at": ts.isoformat(),
            "n_fetches": int(meta.get("n_fetches") or 0) + 1,
            "versions": vers[-20:],
        })
        if extra:
            meta.update(extra)
        tmp = mp + ".tmp"
        io.open(tmp, "w", encoding="utf-8").write(
            json.dumps(meta, ensure_ascii=False, indent=1))
        os.replace(tmp, mp)
        return did, chash, is_new

    def put_text(self, doc_id, text, fields=None, extractor=""):
        """存正文抽取结果。这一层允许覆盖 —— 规则升级要能重跑。"""
        mp = self._find_meta_ym(doc_id)
        ym = mp or _ym()
        p = self._text_path(doc_id, ym)
        os.makedirs(os.path.dirname(p), exist_ok=True)   # 月份子目录可能不存在
        rec = {
            "doc_id": doc_id,
            "text": text or "",
            "text_len": len(text or ""),
            "fields": fields or {},
            "extractor": extractor,
            "extracted_at": datetime.now(timezone.utc).isoformat(),
        }
        tmp = p + ".tmp"
        io.open(tmp, "w", encoding="utf-8").write(
            json.dumps(rec, ensure_ascii=False))
        os.replace(tmp, p)
        return p

    def put_events(self, ym, name, records):
        """存事件层（处理层产物）。可覆盖 —— 规则升级要能重跑。"""
        d = os.path.join(self.root, "extracted/events", ym)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, name + ".json")
        tmp = p + ".tmp"
        io.open(tmp, "w", encoding="utf-8").write(
            json.dumps(records, ensure_ascii=False, indent=1))
        os.replace(tmp, p)
        return p

    # ---------- 读 ----------
    def _find_meta_ym(self, doc_id):
        base = os.path.join(self.root, "raw/meta")
        if not os.path.isdir(base):
            return ""
        for ym in sorted(os.listdir(base), reverse=True):
            if os.path.isfile(os.path.join(base, ym, doc_id + ".json")):
                return ym
        return ""

    def get_meta(self, doc_id):
        ym = self._find_meta_ym(doc_id)
        if not ym:
            return None
        try:
            return json.load(io.open(self._meta_path(doc_id, ym),
                                     encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None

    def get_html(self, doc_id, content_hash=None, rendered=False):
        """取原始 HTML。不给 content_hash 就取最新版本。"""
        meta = self.get_meta(doc_id)
        if not meta:
            return None
        ym = meta["first_fetched_at"][:7].replace("-", "")
        ch = content_hash or meta.get("content_hash")
        p = self._html_path(doc_id, ym, ch, rendered)
        if not os.path.isfile(p):
            # 兼容：早期版本存成 <doc_id>.html.gz
            p2 = self._html_path(doc_id, ym, None, rendered)
            if os.path.isfile(p2):
                p = p2
            else:
                return None
        with io.open(p, "rb") as f:
            data = f.read()
        return gzip.decompress(data) if self.compress else data

    def get_text(self, doc_id):
        ym = self._find_meta_ym(doc_id)
        if not ym:
            return None
        p = self._text_path(doc_id, ym)
        if not os.path.isfile(p):
            return None
        try:
            return json.load(io.open(p, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None

    def has(self, url):
        return bool(self._find_meta_ym(doc_id_of(url)))

    # ---------- 统计 ----------
    def stats(self):
        out = {}
        for sub in ("raw/html", "raw/meta", "extracted/text"):
            d = os.path.join(self.root, sub)
            n = sz = 0
            if os.path.isdir(d):
                for r, _, fs in os.walk(d):
                    for fn in fs:
                        n += 1
                        try:
                            sz += os.path.getsize(os.path.join(r, fn))
                        except Exception:  # noqa: BLE001
                            pass
            out[sub] = {"files": n, "MB": round(sz / 1024 / 1024, 1)}
        return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=r"D:\event-ledger-data")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()
    st = RawStore(a.root)
    if a.stats:
        for k, v in st.stats().items():
            print("  %-22s %6d 文件  %8.1f MB" % (k, v["files"], v["MB"]))
    else:
        print("存储根目录：%s" % st.root)
        print("  压缩：%s" % ("开" if st.compress else "关"))
