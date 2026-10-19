"""把 Astro 构建产物 dist/ 发布到 GitHub Pages 部署仓库。

默认只做本地同步（可 git diff 检查），加 --push 才提交并推送。

    python scripts/deploy.py            # 同步到部署仓，不提交
    python scripts/deploy.py --push     # 同步 + commit + push

部署仓是纯产物仓库（原 Hexo 产物），所以每次同步会先清空（保留 .git）。
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")
DEPLOY = "D:/work/2026/gycxxx.github.io"
# 旧产物不直接删：移到这里留底（同盘移动是瞬时的），git 侧照样记录为删除
BACKUP = "D:/work/2026/_site_backup"


def sync():
    if not os.path.isdir(DIST):
        sys.exit("dist/ 不存在，先跑 npx astro build")
    if not os.path.isdir(os.path.join(DEPLOY, ".git")):
        sys.exit("部署仓不存在或不是 git 仓库: " + DEPLOY)

    import time

    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(BACKUP, stamp)
    os.makedirs(dest, exist_ok=True)

    moved = added = 0
    for name in os.listdir(DEPLOY):
        if name == ".git":
            continue
        shutil.move(os.path.join(DEPLOY, name), os.path.join(dest, name))
        moved += 1

    for name in os.listdir(DIST):
        s, d = os.path.join(DIST, name), os.path.join(DEPLOY, name)
        if os.path.isdir(s):
            shutil.copytree(s, d)
        else:
            shutil.copy2(s, d)
        added += 1

    print("同步完成：旧产物移出 %d 项（备份在 %s），写入 %d 项 -> %s" % (moved, dest, added, DEPLOY))


def push():
    def git(*args):
        return subprocess.run(["git"] + list(args), cwd=DEPLOY, capture_output=True, text=True)

    git("add", "-A")
    r = git("commit", "-m", "site: 切换到 Astro 生成的静态站点")
    if r.returncode != 0:
        print("commit:", r.stdout.strip() or r.stderr.strip())
    for attempt in range(1, 16):
        r = git("push", "origin", "master")
        if r.returncode == 0:
            print("push OK (第 %d 次尝试)" % attempt)
            break
        print("push 失败 %d: %s" % (attempt, (r.stderr or r.stdout).strip()[-120:]))
        import time

        time.sleep(20)
    else:
        sys.exit("push 连续失败，稍后手动重试")
    print(git("log", "--oneline", "-1").stdout.strip())


if __name__ == "__main__":
    sync()
    if "--push" in sys.argv:
        push()
    else:
        print("（未推送。确认无误后加 --push）")
