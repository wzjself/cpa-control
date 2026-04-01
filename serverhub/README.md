# cpa-control

`cpa-control` 是一个面向 CPA 运维场景的 Flask 控制台，集成了：

- 服务器状态面板
- CPA 目标管理
- CPA 凭证扫描 / 刷新 / 清理
- 凭证仓库导入 / 去重 / 投放
- CPA 凭证与仓库之间的互转
- 快照缓存匹配与自动刷新

项目默认监听：

```bash
0.0.0.0:8321
```

---

## 主要功能

### 1. 服务器总览

展示当前服务器：

- CPU
- 内存
- 磁盘
- 网络上下行
- 24h 流量
- clirelay 请求 / Token / RPM / TPM

并带：

- 3h / 24h / 7d / 30d / 全量 历史图表
- 自动刷新

---

### 2. CPA 管理

支持添加、重命名、排序、删除 CPA。

每个 CPA 支持：

- 刷新当前 CPA
- 刷新并扫描全部 CPA
- 展开 / 收起凭证列表
- 一键导出 401
- 一键删除 401
- 一键导出异常凭证
- 一键删除异常凭证

刷新链路走项目内原生刷新逻辑，不依赖外部轮询式二次汇总，速度更快。

---

### 3. CPA 凭证操作

在每个 CPA 的凭证卡片里：

- 左上角可勾选 `✓`
- 勾选后可在上方统一执行：
  - 上传仓库
  - 导出 JSON
  - 删除

适合批量处理，不会在每条凭证后面塞太多按钮。

---

### 4. 凭证仓库

仓库支持：

- 上传本地 JSON / TXT 凭证文件
- 按名称自动去重
- 删除仓库凭证
- 搜索
- 已上传 / 未上传筛选
- 多选后批量上传到目标 CPA
- 根据 CPA snapshot 缓存快速判断某个凭证是否已存在于目标 CPA

仓库刷新时会：

- 自动检查同名重复
- 自动清理历史重复凭证
- 同步当前目标 CPA 的匹配状态

---

### 5. 快照缓存匹配

为了避免切换目标 CPA 时重复实时抓取，项目使用本地 snapshot 缓存做匹配。

特点：

- 切换目标 CPA 秒级返回
- 不在切换时重新访问 CPA 抓名称
- 只在刷新 CPA 时更新名单
- 匹配逻辑和 UI 标注保持一致

---

## 目录说明

```bash
cpa-control/
├── app.py
├── requirements.txt
├── install.sh
├── static/
├── templates/
└── data/
```

### data/

`data/` 是运行时目录，包含：

- SQLite 数据库
- CPA snapshot
- 临时日志
- 401 / quota 输出

这些文件 **不应提交到 Git**。

如果要迁移当前运行状态，需要单独打包 `data/`。

---

## 安装方式

### 方式 1：git clone

```bash
git clone https://github.com/wzjself/cpa-control.git
cd cpa-control
bash install.sh
```

---

### 方式 2：curl 直接下载源码包

仓库公开后，可以直接：

```bash
curl -L https://github.com/wzjself/cpa-control/archive/refs/heads/main.tar.gz -o cpa-control.tar.gz
tar -xzf cpa-control.tar.gz
cd cpa-control-main
bash install.sh
```

---

### 方式 3：curl 下载发布包

如果你使用 GitHub Release 发布包，可直接：

```bash
curl -L <RELEASE_TAR_GZ_URL> -o cpa-control-release.tar.gz
tar -xzf cpa-control-release.tar.gz
cd cpa-control
bash install.sh
```

---

## install.sh 会做什么

`install.sh` 默认会：

- 创建 Python 虚拟环境 `.venv`
- 安装依赖
- 创建 `data/` 目录
- 输出启动命令

执行：

```bash
bash install.sh
```

---

## 启动

### 前台启动

```bash
./.venv/bin/python app.py
```

### 后台启动

```bash
nohup ./.venv/bin/python app.py > serverhub.log 2>&1 &
```

---

## 可选环境变量

```bash
export SERVERHUB_HOST=0.0.0.0
export SERVERHUB_PORT=8321
export CLIRELAY_BASE=http://127.0.0.1:8317
export CLIRELAY_MGMT_KEY=wzjself
```

---

## 适合的使用场景

- 管理多个 CPA 节点
- 快速定位 401 / 限额 / 异常凭证
- 维护凭证仓库
- 将仓库凭证批量投放到指定 CPA
- 从 CPA 中把凭证回收到仓库
- 在单页里同时看服务器状态与 CPA 状态

---

## 注意事项

1. 当前运行数据库文件名仍使用：

```bash
data/serverhub.db
```

这是为了兼容现有线上数据，虽然项目对外名称已经是 `cpa-control`。

2. 如果你只是迁移代码：

- 直接 clone / curl 下载即可

3. 如果你要迁移当前状态：

- 还需要额外同步 `data/`

---

## 一键部署示例

```bash
curl -L https://github.com/wzjself/cpa-control/archive/refs/heads/main.tar.gz -o cpa-control.tar.gz \
  && tar -xzf cpa-control.tar.gz \
  && cd cpa-control-main \
  && bash install.sh
```

---

## License

仅供自用 / 内部运维使用，按你的实际场景自行调整。
