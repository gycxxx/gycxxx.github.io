"""把 Astro 源码推到部署仓的 src 分支，交给 GitHub Actions 构建发布。

部署仓 gycxxx.github.io 是双分支结构：
    master = GitHub Pages 发布产物（由 CI 写入，不要手改）
    src    = Astro 源码（本脚本维护）

流程：master → checkout src → 工作区换成源码 → commit → push
（旧文件用 move 移进备份目录，不用 rm，避免误删与沙箱删除保护）

    python scripts/push_src.py "content: 更新 xxx"
    python scripts/push_src.py            # 用默认提交信息
"""
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY = "D:/work/2026/gycxxx.github.io"
BACKUP = "D:/work/2026/_site_backup"
SKIP = {"node_modules", "dist", ".astro", ".git"}


def git(*args, cwd=DEPLOY):
    return subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True, text=True)


def main():
    msg = sys.argv[1] if len(sys.argv) > 1 else "site: 更新源码"
    if not os.path.isdir(os.path.join(DEPLOY, ".git")):
        sys.exit("部署仓不存在: " + DEPLOY)

    st = git("status", "--porcelain").stdout.strip()
    if st:
        sys.exit("部署仓有未提交改动，先处理干净：\n" + st[:400])

    br = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if br != "src":
        r = git("checkout", "src")
        if r.returncode != 0:
            sys.exit("切到 src 失败: " + (r.stderr or r.stdout).strip()[-200:])
        print("已切换到 src 分支")

    # 用 move 清空工作区（保留 .git）
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(BACKUP, "src_switch_" + stamp)
    os.makedirs(dest, exist_ok=True)
    moved = 0
    for n in os.listdir(DEPLOY):
        if n == ".git":
            continue
        shutil.move(os.path.join(DEPLOY, n), os.path.join(dest, n))
        moved += 1

    copied = 0
    for n in os.listdir(ROOT):
        if n in SKIP:
            continue
        s, d = os.path.join(ROOT, n), os.path.join(DEPLOY, n)
        if os.path.isdir(s):
            shutil.copytree(s, d)
        else:
            shutil.copy2(s, d)
        copied += 1

    print("工作区换血：移出 %d 项（备份 %s），写入 %d 项" % (moved, dest, copied))

    git("add", "-A")
    r = git("commit", "-m", msg)
    if r.returncode != 0:
        print("commit 输出:", (r.stdout or r.stderr).strip()[:200])

    for attempt in range(1, 16):
        r = git("push", "origin", "src")
        if r.returncode == 0:
            print("push OK（第 %d 次尝试），CI 会自动构建并发布到 master" % attempt)
            break
        print("push 失败 %d: %s" % (attempt, (r.stderr or r.stdout).strip()[-120:]))
        time.sleep(20)
    else:
        sys.exit("push 连续失败，稍后重试")
    print(git("log", "--oneline", "-1").stdout.strip())


if __name__ == "__main__":
    main()
