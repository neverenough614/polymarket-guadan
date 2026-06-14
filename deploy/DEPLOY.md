# predict.fun VPS 部署手册

24/7 跑两个进程:**discovery**(每 1h 刷新市场表)+ **live**(选市挂单/监控/清仓/轮换)。

> ⚠️ **铁律**:同一个 predict.fun 账户**绝不能同时跑两个 live**(本地 + VPS 都跑会互相撤挂 → 触发反作弊清零)。
> 部署阶段(装环境/dryrun)不碰挂单,本地可继续跑;**只有最后启动 VPS live 前,必须先停掉本地 live**。

---

## 0. 前置

- Droplet:Ubuntu 24.04,≥2GB 内存,sgp1(新加坡)
- 本地已有 SSH key 加进 droplet

## 1. 上服务器

```bash
ssh root@<DROPLET_IP>
```

## 2. 拉代码(私有库,三选一)

**A. Deploy key(推荐,只读、可吊销)**
```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_deploy -N "" -C "vps-deploy"
cat ~/.ssh/id_deploy.pub      # 复制 → GitHub 仓库 Settings → Deploy keys → Add(只读)
GIT_SSH_COMMAND="ssh -i ~/.ssh/id_deploy" git clone git@github.com:neverenough614/polymarket-guadan.git /root/poly-maker
cd /root/poly-maker && git checkout feature/predictfun-migration
```

**B. PAT**:`git clone https://<PAT>@github.com/neverenough614/polymarket-guadan.git /root/poly-maker`

**C. 本地 scp**(不走 GitHub):本地跑 `scp -r ./ root@<IP>:/root/poly-maker`(注意排除 .venv/.git)

## 3. 配环境(幂等,可重复)

```bash
cd /root/poly-maker
bash deploy/setup_vps.sh
```
装好:2G swap、python venv、全部依赖(含 predict-sdk)。

## 4. 填密钥(⚠️ 只在服务器上做,绝不提交)

```bash
cp deploy/.env.example .env
nano .env            # 填 PREDICTFUN_PK / API_KEY / ACCOUNT / SPREADSHEET_URL
chmod 600 .env
```
再上传 Google 凭据(本地执行):
```bash
scp credentials.json root@<DROPLET_IP>:/root/poly-maker/
```

## 5. 自检(只读,不下单 — 本地还在跑也安全)

```bash
cd /root/poly-maker
./.venv/bin/python scripts/predictfun_update_markets.py dryrun   # 应打印拉取市场+打分,写本地 JSON
./.venv/bin/python scripts/predictfun_run.py plan --limit 10     # 应打印按 PP/USDT 选中的市场
```
两条都正常 = 环境/密钥/网络都通。

## 6. 安装 systemd 服务

```bash
cp deploy/predictfun-discovery.service /etc/systemd/system/
cp deploy/predictfun-live.service /etc/systemd/system/
systemctl daemon-reload
```
> live 服务用 `--limit 10`,想改市场数:`nano /etc/systemd/system/predictfun-live.service` 改 ExecStart,再 `systemctl daemon-reload`。

## 7. 防火墙(先放 SSH 再开,别锁自己)

```bash
ufw allow OpenSSH
ufw --force enable
```

## 8. 🔀 切换上线(关键顺序)

1. **先停本地两个进程**:本地终端各 `Ctrl+C`(本地 live 退出会自动撤单+清仓)。确认本地 live 真的停了。
2. **启 VPS discovery**(先让它写一份新表):
   ```bash
   systemctl enable --now predictfun-discovery
   journalctl -u predictfun-discovery -f      # 看到 [A]拉取市场 / [D]分流 即正常,Ctrl+C 退出查看
   ```
3. **启 VPS live**:
   ```bash
   systemctl enable --now predictfun-live
   journalctl -u predictfun-live -f           # 看到 初挂 + "=== 进入守护循环 ===" 即正常
   ```

## 日常运维

```bash
systemctl status predictfun-live              # 看状态
journalctl -u predictfun-live -f              # 实时日志
journalctl -u predictfun-live --since "1h ago"
systemctl stop predictfun-live                # 停(发 SIGINT → 自动撤单+清仓,给到 120s)
systemctl restart predictfun-live             # 重启(幂等:已挂的不重复挂)
tail -f /var/log/predictfun-live.log          # 文件日志
```

更新代码:
```bash
cd /root/poly-maker && git pull
systemctl restart predictfun-live predictfun-discovery
```

## 安全清单

- [ ] `.env` 权限 600,私钥只在服务器填,从未提交/外传
- [ ] `credentials.json` 已上传、未提交
- [ ] ufw 只放 SSH
- [ ] SSH 用 key 登录(可选:禁用密码登录 `PasswordAuthentication no`)
- [ ] 确认全程只有一个 live 在跑(本地已停)
