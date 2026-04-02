# cpa-control

一个面向 CPA 运维的轻量控制台，默认运行在 **8321** 端口。  
适合用来集中管理多个 CPA、批量处理凭证、查看额度状态、维护本地凭证仓库。

## 主要功能

### 1. CPA 管理
- 添加 / 重命名 / 排序 / 删除 CPA
- 刷新单个 CPA
- 刷新全部 CPA
- 展开 / 收起查看凭证
- 查看凭证状态、剩余额度、下次额度刷新时间

### 2. CPA 凭证管理
- 选择全部 / 选择异常 / 选择 401
- 批量删除 CPA 凭证
- 批量上传到本地仓库
- 批量下载凭证文件
- 收起状态下也支持内部滚动查看凭证

### 3. 本地凭证仓库
- 上传本地凭证文件到仓库
- 自动去重
- 搜索 / 筛选
- 批量上传到目标 CPA
- 查看凭证是否已存在于目标 CPA

### 4. 实时进度面板
批量操作统一使用右侧单框汇总进度显示：
- 已处理 x/y
- 百分比
- 成功 / 失败 / 重复统计

适用于：
- 刷新 CPA
- 删除 CPA 凭证
- CPA 上传仓库
- 仓库上传到 CPA
- 本地文件上传仓库

### 5. 服务器状态面板
- CPU / 内存 / 磁盘 / 网络
- clirelay 统计
- 多时间范围图表

---

## 快速部署

### 方式一：直接安装
```bash
git clone https://github.com/wzjself/cpa-control.git
cd cpa-control
bash install.sh
```

### 方式二：使用发布包
仓库内已提供干净发布包：
- `release/cpa-control.tar.gz`

也可以直接从 GitHub 下载后解压安装：
```bash
curl -L https://github.com/wzjself/cpa-control/raw/master/serverhub/release/cpa-control.tar.gz -o cpa-control.tar.gz
tar -xzf cpa-control.tar.gz
cd cpa-control
bash install.sh
```

### 方式三：手动启动
```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
mkdir -p data
./.venv/bin/python app.py
```

默认监听：
```bash
0.0.0.0:8321
```

后台运行：
```bash
nohup ./.venv/bin/python app.py > serverhub.log 2>&1 &
```

---

## 可选环境变量

```bash
export SERVERHUB_HOST=0.0.0.0
export SERVERHUB_PORT=8321
export CLIRELAY_BASE=http://127.0.0.1:8317
export CLIRELAY_MGMT_KEY=your_key
```

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

说明：
- `data/` 是运行时目录
- 数据库、日志、快照文件不建议提交 Git

---

## 适用场景
- 管理多个 CPA 节点
- 快速定位 401 / 异常 / 限额账号
- 批量处理 CPA 凭证
- 维护本地凭证仓库
- 将仓库凭证批量投放到指定 CPA
