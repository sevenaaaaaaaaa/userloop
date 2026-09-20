"""备份与恢复：SQLite 一致性快照 + 打包 + 保留策略 + 校验.

- **一致性**：运行中的 SQLite 库用 `VACUUM INTO` 生成一致副本（不会拷到半写状态）
- **完整性**：tar.gz + manifest（版本/时间/文件/大小/事件后端），恢复前先校验
- **可回滚**：恢复时先把现有 data_dir 改名留存 `data.bak.<ts>`，再落新数据
- **保留策略**：默认保留最近 7 份，超出自动清理

注意：事件若落在**外部 MySQL**（events 后端=mysql），SQLite 快照不含事件表；
需另行 `mysqldump`（manifest 会记录该情况，见 docs/OPS.md）。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime
from typing import Any

BACKUP_DIR = "backups"
_EXCLUDE_DIRS = {BACKUP_DIR}
_DB_SUFFIX = (".db", ".sqlite", ".sqlite3")
# SQLite 边车文件不能进备份：它们属于「原库」而非 VACUUM 快照，恢复时会污染快照
_SIDECAR_SUFFIX = ("-wal", "-shm", "-journal")


def _ts() -> str:
    """到毫秒的时间戳（同秒内多次备份也能唯一）。"""
    now = datetime.utcnow()
    return now.strftime("%Y%m%d-%H%M%S") + f"-{now.microsecond // 1000:03d}"


def _db_files(data_dir: str) -> list[str]:
    out: list[str] = []
    for root, dirs, files in os.walk(data_dir):
        dirs[:] = [d for d in dirs if d not in _EXCLUDE_DIRS and not d.startswith("data.bak")]
        for f in files:
            if f.endswith(_DB_SUFFIX):
                out.append(os.path.join(root, f))
    return out


def _vacuum_within(src: str, dst: str) -> None:
    """用 VACUUM INTO 生成一致副本（目标必须不存在）。

    用**普通读写连接**而非 `mode=ro`：活库存在 `-wal` 时，只读连接无法完成 WAL 恢复，
    会报 "attempt to write a readonly database"。VACUUM INTO 不修改源库内容，
    读写连接只是允许 SQLite 正常恢复/检查点 WAL。
    """
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        os.remove(dst)
    con = sqlite3.connect(src, timeout=15)
    try:
        con.execute("PRAGMA busy_timeout=15000")
        con.execute("VACUUM INTO ?", (dst,))
    finally:
        con.close()


def events_backend_of(cfg: dict | None) -> str:
    """识别事件后端（与 store.build_event_store 同源：storage.events）。"""
    storage = ((cfg or {}).get("storage") or {}).get("events") or {}
    backend = str(storage.get("backend") or "auto").lower()
    mysql = storage.get("mysql") or {}
    if backend == "mysql" or (backend == "auto" and mysql.get("enabled")):
        return "mysql" if mysql.get("enabled") else "sqlite"
    return "sqlite"


def create(data_dir: str, keep: int = 7, note: str = "", cfg: dict | None = None,
           events_backend: str | None = None) -> dict[str, Any]:
    """创建备份：`<data_dir>/backups/backup-<ts>.tar.gz`。返回路径与元数据。"""
    if not os.path.isdir(data_dir):
        return {"ok": False, "error": f"数据目录不存在：{data_dir}"}
    cfg = cfg or {}
    out_dir = os.path.join(data_dir, BACKUP_DIR)
    os.makedirs(out_dir, exist_ok=True)
    stamp = _ts()
    work = tempfile.mkdtemp(prefix="userloop-backup-")
    members: list[str] = []
    db_info: dict[str, int] = {}
    try:
        stage = os.path.join(work, "data")
        os.makedirs(stage, exist_ok=True)
        dbs = _db_files(data_dir)
        for src in dbs:
            rel = os.path.relpath(src, data_dir)
            dst = os.path.join(stage, rel + ".snapshot")
            _vacuum_within(src, dst)
            db_info[rel] = os.path.getsize(dst)
            # 快照以原文件名落包，恢复时可直接替换
            final = os.path.join(stage, rel)
            os.replace(dst, final)
            members.append(rel)
        for root, dirs, files in os.walk(data_dir):
            dirs[:] = [d for d in dirs if d not in _EXCLUDE_DIRS and not d.startswith("data.bak")]
            for f in files:
                src = os.path.join(root, f)
                rel = os.path.relpath(src, data_dir)
                if src in dbs or rel.startswith(BACKUP_DIR + os.sep):
                    continue
                if f.endswith(_SIDECAR_SUFFIX):
                    continue          # -wal/-shm/-journal 属于原库，不进快照
                dst = os.path.join(stage, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                members.append(rel)
        backend = events_backend or events_backend_of(cfg)
        manifest = {"version": _manifest_version(), "created_at": datetime.utcnow().isoformat() + "Z",
                    "note": note, "data_dir": data_dir, "files": sorted(members),
                    "dbs": db_info, "events_backend": backend,
                    "events_note": ("事件表在外部 MySQL，本 SQLite 快照不含 events；请另行 mysqldump"
                                    if backend == "mysql" else "")}
        with open(os.path.join(stage, "BACKUP-MANIFEST.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        archive = _unique_path(out_dir, f"backup-{stamp}")
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(stage, arcname="data")
        size = os.path.getsize(archive)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    pruned = prune(data_dir, keep=keep)
    return {"ok": True, "path": archive, "name": os.path.basename(archive), "bytes": size,
            "files": len(members), "dbs": db_info, "note": note, "pruned": pruned}


def _unique_path(out_dir: str, base: str) -> str:
    path = os.path.join(out_dir, base + ".tar.gz")
    n = 1
    while os.path.exists(path):
        path = os.path.join(out_dir, f"{base}-{n}.tar.gz")
        n += 1
    return path


def _manifest_version() -> str:
    try:
        from userloop import __version__

        return __version__
    except Exception:  # noqa: BLE001
        return "unknown"


def list_backups(data_dir: str) -> list[dict[str, Any]]:
    out_dir = os.path.join(data_dir, BACKUP_DIR)
    if not os.path.isdir(out_dir):
        return []
    items: list[dict[str, Any]] = []
    for name in sorted(os.listdir(out_dir), reverse=True):
        if not name.endswith(".tar.gz"):
            continue
        path = os.path.join(out_dir, name)
        st = os.stat(path)
        items.append({"name": name, "path": path, "bytes": st.st_size,
                      "created_at": datetime.utcfromtimestamp(st.st_mtime).isoformat() + "Z"})
    return items


def prune(data_dir: str, keep: int = 7) -> list[str]:
    items = list_backups(data_dir)
    removed = []
    for it in items[keep:]:
        try:
            os.remove(it["path"])
            removed.append(it["name"])
        except OSError:
            pass
    return removed


def verify(archive: str) -> dict[str, Any]:
    """校验归档：gzip 可解、manifest 存在、SQLite 快照头合法。"""
    if not os.path.exists(archive):
        return {"ok": False, "error": f"归档不存在：{archive}"}
    problems: list[str] = []
    manifest: dict[str, Any] = {}
    try:
        with tarfile.open(archive, "r:gz") as tar:
            names = tar.getnames()
            manifest_name = next((n for n in names if n.endswith("BACKUP-MANIFEST.json")), None)
            if manifest_name:
                f = tar.extractfile(manifest_name)
                if f:
                    manifest = json.loads(f.read().decode("utf-8"))
            else:
                problems.append("缺少 BACKUP-MANIFEST.json")
            db_names = [n for n in names if n.endswith(_DB_SUFFIX)]
            if not db_names:
                problems.append("归档中未发现 SQLite 库文件")
            for n in db_names:
                f = tar.extractfile(n)
                head = f.read(16) if f else b""
                if not head.startswith(b"SQLite format 3"):
                    problems.append(f"{n} 不是合法 SQLite 文件")
    except (tarfile.TarError, OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"归档损坏：{exc}"}
    return {"ok": not problems, "problems": problems, "manifest": manifest}


def restore(archive: str, data_dir: str, force: bool = False) -> dict[str, Any]:
    """恢复：校验 → 留存现有目录 → 解包覆盖。**恢复后需重启服务**。"""
    if not force:
        return {"ok": False, "error": "恢复会覆盖当前数据，需 force=True 显式确认"}
    v = verify(archive)
    if not v.get("ok"):
        return {"ok": False, "error": f"备份校验失败：{v.get('problems') or v.get('error')}"}
    parent = os.path.dirname(os.path.abspath(data_dir.rstrip("/")))
    base = os.path.basename(os.path.abspath(data_dir.rstrip("/")))
    work = tempfile.mkdtemp(prefix="userloop-restore-")
    try:
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(work)          # 已校验来源为我方生成的归档
        stage = os.path.join(work, "data")
        if not os.path.isdir(stage):
            return {"ok": False, "error": "归档结构异常：缺少 data/ 目录"}
        old = os.path.join(parent, f"{base}.bak.{_ts()}")
        if os.path.isdir(data_dir):
            os.replace(data_dir, old)
        shutil.copytree(stage, data_dir)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {"ok": True, "restored_from": archive, "backup_of_old": old,
            "note": "已恢复，需重启服务使其生效", "manifest": v.get("manifest")}
