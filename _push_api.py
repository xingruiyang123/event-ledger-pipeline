"""通过 GitHub Contents API 推送文件（绕开不稳定的 git over HTTPS）。

git 协议在本次会话中被反复 reset / 超时，但 api.github.com 稳定可达，
所以改用 REST API 逐文件 PUT。适合文件数不多（< 50）的场景。
"""
import base64
import json
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = "xingruiyang123/event-ledger-pipeline"
BRANCH = "main"
ROOT = os.path.dirname(os.path.abspath(__file__))

ENV = dict(os.environ)
ENV["NO_PROXY"] = "*"
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    ENV.pop(k, None)


def gh(args, data=None):
    cmd = ["gh", "api"] + args
    r = subprocess.run(cmd, input=(json.dumps(data) if data else None),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=ENV)
    return r.returncode, r.stdout, r.stderr


def remote_sha(path):
    rc, out, _ = gh([f"repos/{REPO}/contents/{path}?ref={BRANCH}",
                     "--jq", ".sha"])
    if rc == 0 and out.strip() and "null" not in out:
        return out.strip()
    return None


def put(path, content_bytes, message):
    sha = remote_sha(path)
    payload = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode(),
        "branch": BRANCH,
    }
    if sha:
        payload["sha"] = sha
    rc, out, err = gh([f"repos/{REPO}/contents/{path}", "--method", "PUT",
                       "--input", "-"], data=payload)
    if rc != 0:
        return False, (err or out).strip()[:160]
    return True, "ok"


def main():
    # 收集要推的文件（相对 ROOT，跳过 .git）
    files = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
        for fn in filenames:
            if fn.endswith(".pyc"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, ROOT).replace("\\", "/")
            files.append(rel)

    files.sort()
    print("待推 %d 个文件" % len(files))
    ok = fail = 0
    for rel in files:
        with open(os.path.join(ROOT, rel), "rb") as f:
            data = f.read()
        good, msg = put(rel, data, "update: %s" % rel)
        if good:
            ok += 1
            print("  OK   %s" % rel)
        else:
            fail += 1
            print("  FAIL %s -> %s" % (rel, msg))
    print("\n成功 %d / 失败 %d" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
