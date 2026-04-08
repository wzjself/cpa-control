# cpa-control

一个专门用于 **CPA 目标管理、凭证额度读取、凭证批量操作** 的独立控制台。

这个仓库已从原来的 `serverhub` 业务中拆出，核心只保留 CPA 管理相关能力：
- 管理多个 CPA 目标
- 读取目标 CPA 全部凭证
- 识别凭证状态（正常 / 401 / 额度耗尽 / 异常）
- 批量删除、导出、保存到本地仓库
- 从本地仓库批量投放到指定 CPA
- 查看批量任务进度

默认运行端口：`8321`

---

## 核心功能

### 1. CPA 目标管理
- 添加 / 删除 CPA
- CPA 排序
- 单个刷新 / 全部刷新
- 展开查看目标下全部凭证

### 2. 凭证读取与额度识别
- 读取目标 CPA 中的 auth-files
- 标记 401 失效凭证
- 标记额度耗尽凭证
- 标记异常凭证
- 显示剩余额度比例、刷新时间等信息

### 3. CPA 凭证操作
- 按目标批量选择凭证
- 批量删除
- 批量导出
- 批量保存到本地仓库
- 按异常 / 401 / 全部快速筛选

### 4. 本地凭证仓库
- 上传本地 JSON/TXT 凭证文件
- 自动去重
- 搜索 / 筛选
- 批量投放到指定 CPA
- 查看凭证已存在于哪些 CPA

### 5. 批量任务进度
- 刷新 CPA
- 删除凭证
- 保存到仓库
- 仓库投放到 CPA
- 本地文件导入仓库

统一使用右侧进度面板显示执行状态。

---

## 快速部署

### 安装
```bash
git clone https://github.com/wzjself/cpa-control.git
cd cpa-control
bash install.sh
```

### 手动启动
```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
mkdir -p data
./.venv/bin/python app.py
```

### 后台运行
```bash
nohup ./.venv/bin/python app.py > cpa-control.log 2>&1 &
```

访问：
```bash
http://127.0.0.1:8321
```

---

## 环境变量

```bash
export SERVERHUB_HOST=0.0.0.0
export SERVERHUB_PORT=8321
export CLIRELAY_BASE=http://127.0.0.1:8317
export CLIRELAY_MGMT_KEY=your_key
```

说明：
- `SERVERHUB_HOST / SERVERHUB_PORT` 目前是兼容历史代码保留的变量名
- 如后续需要，可以继续重构为 `CPA_CONTROL_HOST / CPA_CONTROL_PORT`

---

## 目录结构

```bash
cpa-control/
├── app.py
├── requirements.txt
├── install.sh
├── static/
├── templates/
└── data/
```

其中：
- `data/` 为运行时目录
- 本地数据库、日志、快照、配额缓存文件均不应提交 Git

---

## 适用场景
- 集中管理多个 CPA 节点
- 快速识别 401 / 限额 / 异常凭证
- 批量处理目标 CPA 内的凭证
- 维护本地凭证仓库
- 将本地仓库凭证批量投放到指定 CPA
