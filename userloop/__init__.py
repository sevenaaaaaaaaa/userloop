"""UserLoop: 全域自动化用户运营工具."""

from pathlib import Path

# 版本唯一来源：仓库根 VERSION 文件（release.sh 只改这一处，避免多处漂移）
try:
    __version__ = (Path(__file__).resolve().parent.parent / "VERSION").read_text(encoding="utf-8").strip()
except OSError:                                   # 打包环境无 VERSION 时的兜底
    __version__ = "0.0.0"

__all__ = ["__version__"]
