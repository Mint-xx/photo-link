# 拍照链接生成

生成专属链接，对方打开自动调用摄像头拍照并上传，凭 6 位数字密码找回照片。

## 功能

- 生成 6 位数字密码的**一次性**拍照链接
- 4 档画质：极省 / 省流 / 标准 / 高清
- 滑动拼图验证码，防止批量刷链接
- 凭密码在首页「查看图片」中找回照片
- 管理后台（管理员 / 游客只读两种角色）
- 管理后台支持单条删除、批量删除、搜索、分页
- 记录打开 IP、拍照 IP、上传速度、各阶段耗时
- 未拍照的链接 24 小时后自动清理

## 技术栈

- **后端**：FastAPI + SQLite（WAL 模式）
- **前端**：原生 HTML / CSS / JavaScript
- **图片压缩**：浏览器端 Canvas / OffscreenCanvas，输出 WebP（回退 JPEG）
- **二维码**：`qrcode[pil]`

## 目录结构

```
photo-link/
├── app.py                 # FastAPI 主应用
├── clear_timing.py        # 清理历史耗时字段的独立脚本（可选）
├── requirements.txt
├── .gitignore
├── README.md
├── templates/
│   ├── index.html         # 首页
│   ├── capture.html       # 拍照页
│   ├── view_auth.html     # 查看照片页
│   ├── admin.html         # 管理后台
│   └── admin_login.html   # 后台登录页
└── uploads/               # 照片存放目录（自动创建）
    └── .gitkeep
```

## 本地运行

```bash
pip install -r requirements.txt

export ADMIN_PASSWORD="你的管理员密码"
export GUEST_PASSWORD="你的游客密码"
export PUBLIC_BASE="https://你的域名"

python app.py
```

默认监听 `127.0.0.1:5004`。

## 环境变量

| 变量名 | 说明 | 默认值 |
|---|---|---|
| `ADMIN_PASSWORD` | 管理员密码 | 空 |
| `GUEST_PASSWORD` | 游客密码（只读） | 空 |
| `PUBLIC_BASE` | 对外访问的完整域名 | 空（使用请求 Host） |

> **注意**：如果 `ADMIN_PASSWORD` 和 `GUEST_PASSWORD` 都为空，后台将无法登录。请务必在部署时通过环境变量注入真实密码。

## 部署

### 首次部署

```bash
cd /www
git clone https://github.com/Mint-xx/photo-link.git
cd photo-link

# 虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 建 uploads 目录（.gitkeep 会被 clone 下来，目录已存在）
# 如果没被 clone 到，手动建：
mkdir -p uploads

# 设置环境变量
export ADMIN_PASSWORD="你的真实管理员密码"
export GUEST_PASSWORD="你的真实游客密码"
export PUBLIC_BASE="https://photo.xiaoxue1.cc.cd"

# 试跑
python app.py
```

### 更新代码

```bash
cd /www/photo-link
git pull

# 如果依赖有变
source .venv/bin/activate && pip install -r requirements.txt

# 重启服务
systemctl restart photo-link   # 或者你用的守护方式
```

### 长期部署

- 发行作品有名为“长期部署所需文件.zip”的文件

### 部署建议

- 用 Nginx 或 Caddy 反向代理 `127.0.0.1:5004`
- 建议前面挂 Cloudflare 或其它 CDN，走 HTTPS
- `uploads/` 目录定期备份，数据库 `data.db` 也建议备份
- 如果并发量大，把日志和事件表改为异步写入（代码里已使用 FastAPI `BackgroundTasks`）

## 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 首页 |
| GET | `/api/create` | 生成链接 |
| GET | `/api/stats` | 首页统计 |
| GET | `/api/quality-stats` | 各档位平均耗时 |
| GET | `/api/qrcode` | 生成二维码 |
| GET | `/api/lookup` | 凭密码查询照片 |
| GET | `/c/{token}` | 拍照页 |
| POST | `/upload/{token}` | 上传照片 |
| GET | `/api/upload-stat/{token}` | 上报各阶段耗时 |
| GET | `/v/{token}` | 照片查看页 |
| GET | `/api/view/{token}` | 验证密码并取图 |
| GET | `/admin` | 后台登录 / 后台首页 |
| POST | `/admin` | 提交后台密码 |
| GET | `/admin/logout` | 退出登录 |
| GET | `/admin/photo/{token}` | 后台查看单张照片 |
| GET | `/admin/photo/file/{token}` | 照片原始文件 |
| GET | `/api/delete/{token}` | 删除单条（仅管理员） |
| POST | `/api/delete-batch` | 批量删除（仅管理员） |

## License

MIT
