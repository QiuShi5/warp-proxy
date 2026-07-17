# warp-proxy

一个基于 Docker 的 Cloudflare WARP 代理容器方案。

容器内启动 Cloudflare WARP，并将 WARP 的本地 SOCKS5 代理（默认 `127.0.0.1:40000`）通过 GOST 转发为：

- SOCKS5 代理：容器内 `1080`
- HTTP 代理：容器内 `8080`
- Web 管理面板：容器内 `8000`

本项目保留原有单节点代理能力，并新增：

- 单 WARP 节点 Web 管理面板
- 多 WARP 节点代理池
- 统一管理容器，提供 HTTP/SOCKS 负载均衡和集群控制台

---

## 功能特性

- 自动安装并启动 Cloudflare WARP
- 以 `proxy` 模式连接 WARP，不直接修改宿主机路由
- 使用 GOST 同时提供 HTTP 代理和 SOCKS5 代理
- 支持通过数据卷持久化 WARP 注册状态
- 支持按时间间隔自动断开重连，以尝试刷新出口 IP
- 支持代理用户名/密码认证
- 支持 Web 面板查看状态、出口 IP、License 池、日志和设置
- 支持多节点 WARP 代理池和统一入口负载均衡

---

## Compose 文件

| 文件 | 用途 | 镜像来源 |
| --- | --- | --- |
| `docker-compose.yml` | 单 WARP 节点，保留原项目默认代理端口 | `ghcr.io/qiushi5/warp-proxy:latest` |
| `docker-compose.cluster.yml` | 3 个 WARP 节点 + 1 个统一管理/负载均衡容器 | `ghcr.io/qiushi5/warp-proxy:latest` |
| `docker-compose.local.yml` | 单 WARP 节点本地构建部署 | 本地 `build: .` |

除 `docker-compose.local.yml` 外，Compose 默认使用 GitHub 自动构建的镜像：

```text
ghcr.io/qiushi5/warp-proxy:latest
```

---

## 项目结构

```text
.
├── backend/                    # Web 管理面板与 API
├── Dockerfile
├── docker-compose.yml          # 单节点默认部署
├── docker-compose.cluster.yml  # 多节点代理池部署
├── docker-compose.local.yml    # 单节点本地构建部署
├── entrypoint.sh
├── requirements.txt
└── tests/
```

---

## 运行要求

在运行前，请确认宿主机满足以下条件：

- 已安装 Docker
- 已安装 Docker Compose（或支持 `docker compose`）
- Linux 环境或支持 `/dev/net/tun` 的 Docker 运行环境
- 允许 WARP 节点容器使用：
  - `NET_ADMIN`
  - `/dev/net/tun`

---

## 环境变量

建议复制示例文件后修改：

```bash
cp .env.example .env
```

示例：

```env
WARP_PROXY_IMAGE=ghcr.io/qiushi5/warp-proxy:latest
PROXY_USER=your_username
PROXY_PASS=your_strong_password
REFRESH_INTERVAL=30
```

### `PROXY_USER`

代理认证用户名。未设置时不会启用用户名认证。强烈建议设置。

### `PROXY_PASS`

代理认证密码。未设置时不会启用密码认证。强烈建议使用强密码。

### `REFRESH_INTERVAL`

WARP 刷新间隔，单位为分钟。

- 正整数：启用自动刷新
- `0`、空值、未设置、非法值：关闭自动刷新

当启用时，容器会每隔对应分钟执行一次：

1. `warp-cli disconnect`
2. `warp-cli connect`

注意：自动刷新会短暂中断已有连接，且不保证一定获得新的出口 IP。

---

## 单 WARP 节点部署

启动：

```bash
docker compose up -d
docker compose logs -f
```

默认端口：

| 服务 | 地址 |
| --- | --- |
| SOCKS5 | `127.0.0.1:1081` |
| HTTP | `127.0.0.1:1082` |
| Web 管理面板 | `http://127.0.0.1:8000` |

数据目录：

```text
warp-data/      # WARP 注册身份缓存
warp-app-data/  # Web 面板设置、License 池和日志
```

如只需要作为其他容器的内部代理节点使用，可以按需删除 `ports` 映射。

---

## 多 WARP 节点代理池部署

启动：

```bash
docker compose -f docker-compose.cluster.yml up -d
docker compose -f docker-compose.cluster.yml logs -f
```

服务组成：

- `warp-1` / `warp-2` / `warp-3`：独立 WARP 节点，各自拥有独立 WARP 注册数据和 License 池
- `warp-manager`：统一入口容器，负责 TCP 轮询负载均衡和统一 Web 控制台

默认端口：

| 服务 | 地址 |
| --- | --- |
| 统一 SOCKS5 入口 | `127.0.0.1:1080` |
| 统一 HTTP 入口 | `127.0.0.1:8080` |
| 统一 Web 管理面板 | `http://127.0.0.1:8000` |

集群数据目录：

```text
cluster-data/warp-1/
cluster-data/warp-2/
cluster-data/warp-3/
```

统一 Web 面板支持：

- 汇总查看所有节点的可达性、WARP 状态、出口 IP 和 License 数量
- 对单个节点连接、断开、轮换、生成 License
- 批量轮换所有节点
- 批量生成 License
- 查看、切换、删除节点 License
- 查看节点日志
- 修改节点自动轮换与健康检查设置
- 查看 HTTP/SOCKS 负载均衡目标健康状态与连接统计

`warp-manager` 不运行 WARP，不需要 `/dev/net/tun`。

---

## 单节点本地构建部署

用于本地开发或 PR 测试：

```bash
docker compose -f docker-compose.local.yml up -d --build
docker compose -f docker-compose.local.yml logs -f
```

这是唯一包含 `build: .` 的 Compose 文件。

---

## 使用方法

### 单节点 SOCKS5

```bash
curl --socks5 127.0.0.1:1081 -U "$PROXY_USER:$PROXY_PASS" https://ifconfig.me
```

### 单节点 HTTP

```bash
curl -x "http://$PROXY_USER:$PROXY_PASS@127.0.0.1:1082" https://ifconfig.me
```

### 集群 SOCKS5

```bash
curl --socks5 127.0.0.1:1080 -U "$PROXY_USER:$PROXY_PASS" https://ifconfig.me
```

### 集群 HTTP

```bash
curl -x "http://$PROXY_USER:$PROXY_PASS@127.0.0.1:8080" https://ifconfig.me
```

---

## 扩容集群节点

在 `docker-compose.cluster.yml` 中复制一个节点：

```yaml
warp-4:
  <<: *warp-node
  container_name: warp-node-4
  volumes:
    - ./cluster-data/warp-4/warp:/var/lib/cloudflare-warp
    - ./cluster-data/warp-4/data:/data
```

然后在 `warp-manager.environment` 中追加 `warp-4`：

```yaml
- CLUSTER_NODES=warp-1=http://warp-1:8000,warp-2=http://warp-2:8000,warp-3=http://warp-3:8000,warp-4=http://warp-4:8000
- SOCKS_TARGETS=warp-1=warp-1:1080,warp-2=warp-2:1080,warp-3=warp-3:1080,warp-4=warp-4:1080
- HTTP_TARGETS=warp-1=warp-1:8080,warp-2=warp-2:8080,warp-3=warp-3:8080,warp-4=warp-4:8080
```

重新启动：

```bash
docker compose -f docker-compose.cluster.yml up -d
```

---

## 安全建议

### 1. 一定要启用认证

如果不设置 `PROXY_USER` 和 `PROXY_PASS`，代理可能以无认证方式对外提供服务。

至少应设置：

```env
PROXY_USER=your_user
PROXY_PASS=please_use_a_long_random_password
```

### 2. 限制监听地址

默认 Compose 绑定 `127.0.0.1`，避免直接暴露到局域网或公网。

如果要给局域网设备使用，可以改成宿主机内网 IP。不建议直接监听 `0.0.0.0`，除非你明确知道暴露范围并配置了防火墙。

### 3. 使用强密码

密码建议：

- 至少 16 位
- 包含大小写字母、数字、符号
- 不与其他服务复用

### 4. 不要提交真实凭据

`.env` 不应提交到公开仓库。真实部署凭据请使用本地 `.env`、私有配置或 secret 管理系统。

### 5. 谨慎使用自动刷新

频繁断开/重连可能导致：

- 当前连接中断
- 应用重试增多
- 日志噪音增加

如果主要目标是稳定代理，建议关闭或适当拉长刷新周期。

---

## 常见操作

### 查看状态

```bash
docker compose ps
```

### 查看实时日志

```bash
docker compose logs -f
```

### 重启容器

```bash
docker compose restart
```

### 停止并删除容器

```bash
docker compose down
```

### 清理单节点 WARP 数据

```bash
rm -rf ./warp-data ./warp-app-data
docker compose up -d
```

### 清理集群 WARP 数据

```bash
rm -rf ./cluster-data
docker compose -f docker-compose.cluster.yml up -d
```

---

## 故障排查

### 1. 容器启动失败，提示 `/dev/net/tun` 相关错误

请检查：

- 宿主机是否支持 TUN
- Docker 是否允许映射 `/dev/net/tun`
- `devices` 配置是否正确

### 2. WARP 无法连接

排查方向：

- 宿主机网络是否正常
- 容器是否能访问 Cloudflare 服务
- 是否存在 DNS / 防火墙限制
- 是否是 WARP 服务端临时异常

### 3. 代理端口无响应

检查：

- 容器是否正常运行
- 端口映射是否正确
- 监听 IP 是否写错
- 宿主机防火墙是否拦截
- GOST 是否正常启动

### 4. 认证无效或代理未要求密码

请确认：

- `PROXY_USER` 与 `PROXY_PASS` 是否都已设置
- Compose 修改后是否已重新创建容器

建议执行：

```bash
docker compose down
docker compose up -d
```

### 5. 集群面板显示节点不可达

请检查：

- 对应 `warp-*` 节点容器是否运行
- `warp-manager` 与节点是否在同一 Docker 网络
- `CLUSTER_NODES` 中的节点名和端口是否正确

---

## 已知注意事项

- 当前项目依赖 Cloudflare WARP 官方 Linux 客户端
- 当前项目依赖 GOST `v2.11.5`
- WARP 以 `proxy` 模式运行，目标是提供代理出口，而不是接管整个宿主机网络
- `warp-data` 或 `cluster-data` 中保存的是 WARP 本地状态，删除后可能触发重新注册

---

## 免责声明

请仅在你有权使用的网络环境和合规场景下使用本项目，并自行承担部署、访问控制与安全加固责任。
