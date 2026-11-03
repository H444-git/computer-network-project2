# 部署说明

演示环境（Windows 本机）不需要本文；下面是把三端拆到真实设备上的做法。

```text
手机（任意网络）──HTTPS/HTTP──▶ VPS: relay.py + 网页控制台
                                     ▲
树莓派/路由器/主机: gateway.py ───────┘ 反向连接（无需端口映射）
```

## 一、公网 VPS 部署中转

以 Ubuntu/Debian 为例，假设 VPS 公网 IP 为 `203.0.113.10`。

```bash
sudo apt update && sudo apt install -y python3
sudo useradd --system --home /opt/humiao --shell /usr/sbin/nologin humiao
sudo mkdir -p /opt/humiao
sudo cp common.py hub.py relay.py webconsole.py /opt/humiao/
sudo cp -r web /opt/humiao/
sudo chown -R humiao:humiao /opt/humiao

# 修改默认令牌后再启动
sudo sed -i 's/WEB_TOKEN=parent123/WEB_TOKEN=换成你自己的令牌/' deploy/relay.service
sudo cp deploy/relay.service /etc/systemd/system/relay.service
sudo systemctl daemon-reload
sudo systemctl enable --now relay

sudo ufw allow 9000/tcp   # 网关反向连接
sudo ufw allow 8080/tcp   # 网页控制台（建议改为仅 HTTPS 反代开放）
```

查看状态与日志：

```bash
systemctl status relay
journalctl -u relay -f
```

## 二、树莓派 / Linux 主机部署网关

网关需要 root 才能操作 iptables。

```bash
sudo apt update && sudo apt install -y python3 iptables
sudo mkdir -p /opt/humiao
sudo cp common.py gateway.py /opt/humiao/
sudo chown -R root:root /opt/humiao

sudo cp deploy/gateway.service /etc/systemd/system/gateway.service
sudo nano /etc/systemd/system/gateway.service   # 改 RELAY_HOST / DEVICE_ID
sudo systemctl daemon-reload
sudo systemctl enable --now gateway

sudo iptables -w -L OUTPUT -n --line-numbers   # 检查规则是否出现
```

验证：在网页控制台阻断一个域名后，网关机器的出站访问应被拦截；解除后恢复。

> 注意：规则只作用于 `OUTPUT` 链，即**本机发出的流量**。如果网关是旁路设备而不是家庭出口路由器，需要改为在转发链（`FORWARD`）上配置，或把网关直接作为主路由。

## 三、OpenWrt 路由器

1. 安装 Python（包名随 OpenWrt 版本不同，常见为 `python3-light`，必要时补 `python3-asyncio`）：

   ```sh
   opkg update
   opkg install python3-light python3-asyncio iptables-nft
   ```

2. 拷贝 `common.py`、`gateway.py` 到 `/root/humiao/`。
3. 先用前台运行排查：

   ```sh
   RELAY_HOST=203.0.113.10 DEVICE_ID=child-gw USE_REAL_IPTABLES=1 python3 gateway.py
   ```

4. 确认无误后，用 procd 或 `luci-app-commands` 配置开机启动；确保 `iptables` 命令可用，且路由器以 root 运行。
5. 路由器存储空间较小时，可只保留 `common.py` 和 `gateway.py`。

## 四、手机在外网访问控制台

- 最简单：手机浏览器打开 `http://203.0.113.10:8080`。
- 推荐：用域名 + HTTPS 反向代理，避免令牌在公网明文传输。

Caddy 示例（自动签发证书）：

```caddyfile
console.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

Nginx 示例（注意关闭 SSE 缓冲）：

```nginx
server {
    listen 443 ssl;
    server_name console.example.com;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_buffering off;        # SSE 必须
        proxy_read_timeout 1h;
    }
}
```

使用 HTTPS 后，把 VPS 防火墙的 8080 只对 127.0.0.1 开放。

## 五、部署安全清单

- [ ] 修改 `WEB_TOKEN`，不要使用默认的 `parent123`
- [ ] 公网只暴露 9000（网关）和 443（HTTPS 控制台）；8080 不直接对公网开放
- [ ] 中转使用非 root 用户运行
- [ ] 网关以 root 运行是 iptables 所需，尽量只在这台设备上开放必要服务
- [ ] 定期查看 `gateway_actions.log` 和 `journalctl -u relay`
- [ ] 不要把中转的 SSH、控制台和网关使用同一个弱密码

## systemd 单元

- `deploy/relay.service`：中转 + 网页控制台
- `deploy/gateway.service`：家庭网关

修改文件中的 `Environment=` 行即可调整端口、中转地址、设备号和令牌。
