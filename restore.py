# =============================================================
# [落落定制] 开机自动恢复: 从 OMBRE_BACKUP_REPO 拉回 buckets/
# 免费层无持久盘 → 容器每次重建 buckets/ 都是空的。
# 惰性触发: /health 首次被访问时拉起 (FastMCP 无 startup 钩子,
# 仿照 _ensure_family_auto_rebuild 的既有模式)。
# 纯 dulwich 实现, 不依赖系统 git。
# 阀门: OMBRE_RESTORE=off 关闭; OMBRE_RESTORE_FORCE=1 强制重拉。
# =============================================================
import os
import shutil
import tempfile
import threading

_restore_done = False
_restore_lock = threading.Lock()


def _log(msg):
    try:
        from server import logger
        logger.info(f"[restore] {msg}")
    except Exception:
        print(f"[restore] {msg}", flush=True)


def restore_from_repo(force=False):
    """把备份仓的 buckets/ 克隆回本地 buckets_dir。只补缺, 不覆盖新文件。
    免费层重建 = 全空 → 全量拉回。半空(有新写入)时按 mtime 合并。"""
    repo_url = os.environ.get("OMBRE_BACKUP_REPO", "").strip()
    token = os.environ.get("OMBRE_BACKUP_TOKEN", "").strip()
    if not (repo_url and token):
        _log("OMBRE_BACKUP_REPO/TOKEN 未配置, 跳过恢复")
        return False
    if "https://" not in repo_url:
        _log("REPO 必须是 https:// URL, 跳过恢复")
        return False
    auth_url = repo_url.replace("https://", f"https://x-access-token:{token}@", 1)
    try:
        from dulwich import porcelain
    except ImportError as e:
        _log(f"dulwich 未安装: {e}")
        return False

    from server import config
    buckets_dir = config.get("buckets_dir", "./buckets")

    tmp = tempfile.mkdtemp(prefix="ombre-restore-")
    try:
        _log(f"从 {repo_url} 浅克隆...")
        porcelain.clone(
            auth_url, tmp,
            depth=1,
            checkout=True,
            errstream=open(os.devnull, "wb"),
        )
        src = os.path.join(tmp, "buckets")
        if not os.path.isdir(src):
            _log("备份仓无 buckets/ 目录 (首次部署?), 跳过")
            return False
        os.makedirs(buckets_dir, exist_ok=True)
        n_new, n_kept = 0, 0
        for root, dirs, files in os.walk(src):
            rel_root = os.path.relpath(root, src)
            dst_root = buckets_dir if rel_root == "." else os.path.join(buckets_dir, rel_root)
            os.makedirs(dst_root, exist_ok=True)
            for f in files:
                s = os.path.join(root, f)
                d = os.path.join(dst_root, f)
                if not os.path.exists(d):
                    shutil.copy2(s, d)
                    n_new += 1
                else:
                    # 双方都在: 留较新的一份 (本地可能是唤醒后新写入的)
                    if os.path.getmtime(s) > os.path.getmtime(d):
                        shutil.copy2(s, d)
                        n_new += 1
                    else:
                        n_kept += 1
        # runtime_config.json 跟 buckets/ 同级, 一起拉回
        rc = os.path.join(tmp, "runtime_config.json")
        if os.path.isfile(rc):
            shutil.copy2(rc, os.path.join(os.path.dirname(buckets_dir), "runtime_config.json"))
        _log(f"恢复完成: 新拉 {n_new} 个文件, 保留本地 {n_kept} 个")
        return True
    except Exception as e:
        _log(f"恢复失败: {type(e).__name__}: {e}")
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def ensure_restore():
    """惰性入口: 全程只跑一次 (除非 OMBRE_RESTORE_FORCE=1)。"""
    global _restore_done
    if _restore_done and os.environ.get("OMBRE_RESTORE_FORCE", "") != "1":
        return
    if os.environ.get("OMBRE_RESTORE", "on").strip().lower() == "off":
        _restore_done = True
        return
    with _restore_lock:
        if _restore_done:
            return
        _restore_done = True
        # 后台线程跑, 不阻塞首个请求
        threading.Thread(target=restore_from_repo, daemon=True, name="ombre-restore").start()
