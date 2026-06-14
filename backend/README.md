# auto_trade backend

## 快速开始

### 1. 准备 Python 3.11+

```bash
python3.11 --version    # 必须 3.11+（CLAUDE.md 强约束）
```

### 2. 重建虚拟环境

```bash
# 用 requirements.txt 的 ~= 范围（推荐开发环境）
./scripts/setup_venv.sh --reset

# 用 requirements.lock.txt 的精确版本（推荐 CI / 生产）
./scripts/setup_venv.sh --reset --locked
```

> **如果遇到 `bad interpreter` 或 venv 指向 host 路径**：说明 `.venv/bin/python` 符号链接损坏了（sandbox / 不同机器之间 venv 不可移植）。`--reset` 会删除重建。

### 3. 激活 & 验证

```bash
source .venv/bin/activate
python -c "import fastapi, sqlalchemy, pydantic; print('ok')"
pytest tests/test_database.py -q     # 冒烟测试
pytest tests/ -v                     # 全部测试
```

### 4. 启动 API

```bash
uvicorn app.main:app --reload --port 8000
```

## 依赖管理

| 文件 | 用途 |
|------|------|
| `requirements.in` | pip-compile 的输入源（手维护） |
| `requirements.txt` | `~=` 范围约束（适合日常开发） |
| `requirements.lock.txt` | `==` 精确锁定（适合 CI / 生产） |
| `requirements-dev.txt` | pytest / basedpyright 等开发工具 |

### 更新 lock

```bash
pip install pip-tools
pip-compile requirements.in --generate-hashes --output-file=requirements.lock.txt
```

## SQLite 注意事项

`backend/app/database.py` 已启用以下 PRAGMA（P46）：

- `journal_mode=WAL` — 并发读写（runner 线程 + FastAPI 处理器）
- `busy_timeout=5000` — 写锁等待最多 5 秒
- `synchronous=NORMAL` — WAL 模式下足够安全
- `foreign_keys=ON` — 显式启用外键

**备份**：直接复制 `data/auto_trade.db`（SQLite 文件，WAL 模式下也需要 `-wal` / `-shm` 一起备份，否则可能有未刷盘数据）。

## 已知问题

详见 `/docs/Roadmap.md` 与各 `P##` 迭代规格文档。
