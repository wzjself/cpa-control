# cpa-control

一个专门用于 **CPA 目标管理、凭证额度读取、凭证批量操作** 的独立控制台。

这个仓库已从原来的 `serverhub` 业务中拆出，核心只保留 CPA 管理相关能力：
- 管理多个 CPA 目标
- 读取目标 CPA 全部凭证
- 识别凭证状态（正常 / 401 / 额度耗尽 / 异常）
- 批量删除、导出、保存到本地仓库
- 从本地仓库批量投放到指定 CPA
- 查看批量任务进度
- 不包含服务器总览、图表、历史指标、clirelay 仪表盘

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

### 方式一：宿主机一键安装
> 这不是 Docker 部署，而是直接安装到宿主机目录并创建 Python 虚拟环境。

```bash
curl -fsSL https://raw.githubusercontent.com/wzjself/cpa-control/main/bootstrap.sh | bash
```

默认安装目录：
```bash
/opt/cpa-control
```

自定义安装目录：
```bash
curl -fsSL https://raw.githubusercontent.com/wzjself/cpa-control/main/bootstrap.sh | CPA_CONTROL_DIR=/your/path bash
```

---

### 方式二：Docker 一键部署
> 如果你要部署到 Docker，用这条，不要用上面的宿主机安装脚本。

```bash
curl -fsSL https://raw.githubusercontent.com/wzjself/cpa-control/main/bootstrap-docker.sh | bash
```

默认也是部署到：
```bash
/opt/cpa-control
```

自定义安装目录：
```bash
curl -fsSL https://raw.githubusercontent.com/wzjself/cpa-control/main/bootstrap-docker.sh | CPA_CONTROL_DIR=/your/path bash
```

部署完成后默认访问：
```bash
http://127.0.0.1:8321
```

---

### 方式三：源码安装
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
```

说明：
- 当前仅保留应用监听地址和端口
- 变量名为兼容历史代码保留，后续可再改名

---

## 当前项目结构

```bash
cpa-control/
├── app.py                 # Flask 入口，注册页面与 API 路由
├── services/
│   ├── __init__.py
│   └── core.py            # 核心业务：CPA、凭证仓库、额度识别、批量任务
├── static/                # 前端脚本与样式
├── templates/             # HTML 模板
├── data/                  # 运行时数据目录
├── README.md
├── requirements.txt
└── install.sh
```

### 模块职责

#### app.py
负责：
- Flask 应用入口
- 页面路由
- API 路由注册
- 启动服务

#### services/core.py
负责：
- CPA 目标管理
- 目标凭证读取
- 额度/状态识别
- 本地凭证仓库管理
- 批量上传、删除、导出、保存到仓库
- 进度任务数据维护

### 后续可继续细拆的方向
如果后面项目继续变大，建议再拆成：
- `services/cpa_service.py`
- `services/credential_service.py`
- `services/task_service.py`
- `services/quota_service.py`

当前这版先保持“可维护 + 可运行”，避免一次拆太散影响稳定性。

---


## 适用场景
- 集中管理多个 CPA 节点
- 快速识别 401 / 限额 / 异常凭证
- 批量处理目标 CPA 内的凭证
- 维护本地凭证仓库
- 将本地仓库凭证批量投放到指定 CPA
