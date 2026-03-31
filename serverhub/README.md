# cpa-control

一个用于服务器状态、CPA 管理、凭证仓库管理的 Flask 项目。

## 运行

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python app.py
```

默认监听：

```bash
0.0.0.0:8321
```

## 部署到另一台服务器

```bash
git clone <YOUR_GITHUB_REPO> cpa-control
cd cpa-control
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python app.py
```

## 说明

- 运行时数据保存在 `data/`
- `data/` 下的数据库、快照、日志不应提交到 Git
- 如需迁移当前数据，请单独打包 `data/` 目录
