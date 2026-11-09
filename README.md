# AppHub 应用聚合中心（飞牛 fnOS 原生应用）

把本机所有 Docker 应用的 Web 入口聚合成一个页面，自动扫描容器暴露端口，支持在应用内直接内嵌浏览。

- 当前版本：1.0.15
- 安装包：`apphub_1.0.15.fpk`（飞牛应用中心 / FnDepot 均可安装）
- 端口：9080（host 网络）
- 数据目录：`/var/apps/apphub/shares/apphub/data`（SQLite 数据库 `apphub.db`）

## 安装

### 方式一：FnDepot 第三方源（可自动更新）

1. 打开 FnDepot → 设置 → 外部应用源 → 添加源。
2. 填入仓库地址：

   ```text
   https://github.com/TaroCats/FN-AppHub
   ```

3. 同步后即可在 FnDepot 中看到「应用聚合中心」，后续新版本会直接提示升级（直接安装 fpk，数据保留）。

源索引是仓库根目录的 [`fnpack.json`](fnpack.json)，遵循 FnDepot 外部应用源 V2 规范（`schema_version: "2"`），FPK 从 GitHub Release 下载并带 `sha256` 与 `size` 校验。
如果客户端版本较旧、无法识别仓库地址，也可以直接添加 JSON 直链：

```text
https://raw.githubusercontent.com/TaroCats/FN-AppHub/main/fnpack.json
```

### 方式二：手动安装

1. 从 [Releases](https://github.com/TaroCats/FN-AppHub/releases) 下载最新的 `apphub_x.y.z.fpk`。
2. 飞牛桌面 → 应用中心 → 右上角「…」→ 手动安装应用（或直接双击 fpk）。
3. 安装过程会基于本地源码构建镜像 `apphub:x.y.z`（首次需拉取 `python:3.12-slim`，请保持联网）。
4. 桌面出现「应用聚合中心」图标，点击后在飞牛桌面窗口内加载。

> 升级：直接安装新版本 fpk 覆盖升级，访问列表保留在 `/var/apps/apphub/shares/apphub/data/apphub.db`。
> 若安装后图标点击无反应，检查 9080 是否被占用：`ss -lntp | grep 9080`。

## 功能

| 功能 | 说明 |
| --- | --- |
| 自动扫描 | 读取 `/var/run/docker.sock`，列出所有运行中容器的对外端口，自动挑选最像 Web 的端口（80/443/8080/3000/5244 等优先），默认过滤 80/443 与内部服务 |
| 批量导入 | 扫描抽屉里勾选多个容器，一键加入访问列表；已收录的标记「已添加」 |
| 手动新增 | 名称、地址、端口、协议、分组、图标，支持任意内网/外网 URL |
| 访问列表管理 | 编辑、删除、分组、搜索（按名称/地址/分组） |
| 应用内浏览 | 每个条目可在 AppHub 窗口内浏览，顶部 tab 可切「内嵌（代理）」与「直连」；打开方式（窗口内 / 新标签页）与浏览模式是两个独立字段 |
| 在线状态 | 后台定时探测目标可达性，卡片上显示在线/离线 |
| 图标抓取 | 自动尝试抓取目标站点 favicon，失败则用首字母色块 |

## 构建与发布（GitHub Actions）

工作流：[`.github/workflows/build-fpk.yml`](.github/workflows/build-fpk.yml)

| 触发方式 | 行为 |
| --- | --- |
| push 到 `main` / Pull Request | 用官方 `fnpack` 打包并校验包内 manifest，仅上传 workflow artifact，不发布 |
| push 标签 `v*` | 打包 → 创建/更新同名 Release 并上传 fpk → 重写 `fnpack.json` 的版本节点（`sha256`/`size`/`changelog`）→ 回写 `main` |
| 手动 `workflow_dispatch` | 可选 `none/patch/minor/major` 递增版本号，随后与 tag 发布流程一致 |

发布一个新版本的两种做法：

```bash
# A. 手动改版本号后打标签
#    编辑 manifest 的 version 与 app/docker/docker-compose.yaml 的 image tag
git commit -am "chore: bump 1.0.15"
git tag v1.0.15 && git push origin main --tags
```

```text
B. 在 Actions 页面选择 Build FPK → Run workflow → bump 选 patch
   （工作流会自动改 manifest / compose 版本号、打包、发 Release、更新 fnpack.json 并回写 main）
```

本地打包（需先安装 [fnpack](https://static2.fnnas.com/fnpack/fnpack-1.2.3-linux-amd64)）：

```bash
chmod +x fnpack-1.2.3-linux-amd64 && sudo mv fnpack-1.2.3-linux-amd64 /usr/local/bin/fnpack
fnpack build          # 仓库根目录执行，产出 apphub_<version>.fpk
```

单独更新源索引（一般由工作流调用，无需手工执行）：

```bash
python3 scripts/gen_fnpack.py --version 1.0.13 \
  --fpk apphub_1.0.13.fpk \
  --download-url https://github.com/TaroCats/FN-AppHub/releases/download/v1.0.13/apphub_1.0.13.fpk
```

> `fnpack.json` 中的 `sha256` 必须与 Release 上的 fpk 完全一致，否则 FnDepot 会校验失败并跳过该版本。请始终让工作流生成，不要手工编造哈希。

## 1.0.15 变更

- **统一配置改为「固定域名 + 映射 IP」**：选择统一配置的应用，地址只保存 `协议://fnos.local:端口`，不再把当时的 IP 写进每个应用。
  - 主面板「访问地址」保存的是 **fnos.local 的映射 IP**，服务端把它写入容器 `/etc/hosts`（带 `# apphub-managed` 标记），容器重建后按库中记录自动恢复。
  - 换 IP、换网段时只改这一处映射，所有统一配置应用立即跟随，不再逐个改地址，也不会产生一堆积压的旧 IP。
  - 旧版本升级后，库中已有的统一配置应用地址会在启动时自动收敛为 `fnos.local`，端口与主页路径保持不变。
- **编辑弹窗不再要求填写 IP**：主机固定显示 `fnos.local`，只填端口即可；预览行会提示当前 fnos.local 映射到哪个 IP。
- **「访问地址」弹窗语义调整**：标题改为「统一配置访问地址（映射 IP）」，只保存映射，不再批量替换各应用主机；自定义地址的应用不受影响。
- **直连说明**：`fnos.local` 由 AppHub 在容器内解析，内嵌浏览（代理/直连）均可用；在其它设备的新标签页直连时，需要该设备能解析 `fnos.local`（fnOS 默认 mDNS）或在系统 hosts 中手动指向 NAS IP。
- 后端 `rebind-host` / `settings/base-host` 改为「保存映射」语义，扫描收录的地址同样落成 `fnos.local:端口`，重复检测按别名地址判断。

## 1.0.14 变更

- **卡片地址按填写方式区分显示**：
  - 走统一配置主机的应用（`统一配置` 模式）只显示端口与主页路径，如 `:8096/web`，不再重复显示每张卡都相同的主机 IP。
  - 自定义完整地址的应用全量显示 `协议://主机:端口/路径`，如 `http://192.168.31.50:9000/panel`。
- 自定义地址允许换行（最多两行），长域名不再被单行截断；悬停仍可看到完整打开地址。
- 端口为默认端口（80 / 443）或无显式端口时，按实际保存值展示，不丢信息。

## 1.0.13 变更

- **访问地址改为两种填写方式**（编辑弹窗顶部单选）：
  - `统一配置`（默认）：只需填协议与端口，主机自动跟随主面板「访问地址」/ 扫描抽屉选定的 IP，保存时拼成 `http://IP:端口`；主面板换 IP 后，这类应用地址会一起改，不用逐个编辑。
  - `自定义`：直接填完整地址（可带协议、域名、端口、路径），与主面板主机无关。
- **编辑弹窗地址区重排**：统一配置下为 `协议下拉 + 主机（只读）+ 端口`，下方实时预览「将保存为 http://…:端口」；切到自定义时自动把当前地址带入输入框。
- **新增「主页地址」字段（选填）**：主页在子路径（如 `/admin`）时直接填 `/admin`，点卡片、新窗口、内嵌代理都会带上该路径（内嵌为 `/p/{id}/admin`），卡片副标题显示 `IP:端口/路径`。
- **后端结构升级**：`apps` 表新增 `url_mode` / `port` / `home_path`，旧库启动自动迁移；统一配置模式支持只填端口，由服务端按已记录主机兜底拼接。
- **面板改主机同步生效**：主面板「访问地址」写入服务端 `base_host`，统一配置模式的应用批量跟随。

## 1.0.12 变更

- 修复编辑弹窗分组下拉样式：改为自绘 combobox，宽度与输入框一致、跟随弹窗圆角与主题色、暗色模式自适应。
- 聚焦展开全部已有分组、输入即过滤、上下键移动、回车选中、Esc 仅收起面板、点击外部或 Tab 收起，支持直接输入新分组。
- 底部空间不足时面板自动向上展开。

## 1.0.11 变更

- 新增「分组管理」入口：新建、重命名、删除、上下排序分组，列表右侧显示每组应用数量。
- 后端新增 `GET/POST /api/groups`、`/api/groups/rename`、`/api/groups/delete`、`/api/groups/reorder`；新增 `groups` 表并在启动时把历史 `group_name` 回填为初始分组。
- 重命名联动组内应用；删除分组只把应用移到「未分组」，「未分组」禁止改名与删除。

## 1.0.10 变更

- 修复应用中心安装报 10266（容器名冲突）：`cmd/install_init` 与 `cmd/upgrade_init` 在安装/升级前读取 compose 的 `container_name`，宿主已存在同名容器则 `docker rm -f` 后交给应用中心创建。
- 容器名、端口、数据目录均未改动。

## 1.0.9 变更

- 「打开方式」与「模式」彻底解耦：`open_mode` 只决定点击卡片后去哪儿（内嵌打开 / 新窗口打开），`mode` 只决定浏览页顶部 tab 默认值（代理 / 直连）。
- 模式字段常显，选「新窗口打开」时同样可设置并保存。

## 1.0.8 变更

- 「打开方式」拆成两层：打开方式（内嵌 / 新窗口）与内嵌模式（代理 / 直连）。
- 内嵌模式作为浏览页顶部 tab 的默认值，切 tab 会写回该应用。
- 旧库启动自动补列 `open_mode`，历史数据默认 `inline`；`PUT /api/apps/{id}` 改为 `exclude_unset`。

## 1.0.6 / 1.0.7 变更

- 1.0.6：在内嵌浏览里切换「内嵌 / 直连」会写回应用的 `mode` 字段，编辑弹窗回显当前存储值。
- 1.0.7：修正「直连」语义为绕开反向代理、在窗口内用应用原始地址加载（已被 1.0.8 取代）。

## 1.0.5 变更

- 修复批量操作弹窗里的复选框尺寸：`.field .checkline input[type="checkbox"]` 固定 15×15，`accent-color` 跟随主题色。

## 1.0.4 变更

- 新增 `POST /api/apps/rebind-host`，按主机名批量改写地址并保留协议、端口与路径。
- 顶部新增「访问地址」批量入口，可选只处理指向回环地址的条目。

## 1.0.3 变更

- 扫描默认使用访问地址：导入的应用地址默认取浏览器访问飞牛时用的主机名，手机/其它电脑点开即通。
- `/api/scan` 支持 `host` 参数，未传时按默认主机 → `X-Forwarded-Host`/`Host` → `127.0.0.1` 兜底，IPv6 自动加方括号；健康探测仍走 `127.0.0.1`。

## 1.0.2 变更

- 修复空状态误显示：全局补 `[hidden] { display: none !important; }`。
- 修复卡片菜单被裁剪：更多菜单改为 body 级固定定位浮层，按视口位置自动向上展开。

## 1.0.1 变更

- 桌面入口改为窗口内打开：`app/ui/config` 的 `type` 由 `url` 改为 `iframe`。
- 代理注入的运行时补丁新增 `window.open` 与 `a[target=_blank]` 拦截，避免内嵌页跳出飞牛窗口。
- 容器端口改为读取 `APPHUB_PORT` 环境变量。

## 目录结构

```text
FN-AppHub/
├── manifest                 # 应用元信息（appname/version/service_port=9080）
├── ICON.PNG / ICON_256.PNG  # 应用中心图标
├── assets/apphub.png        # FnDepot 源图标
├── fnpack.json              # FnDepot 第三方源索引（V2），由工作流自动维护
├── scripts/gen_fnpack.py    # 生成/更新源索引
├── .github/workflows/       # GitHub Actions 打包发布
├── app/
│   ├── ui/                  # 桌面入口配置（iframe 类型，端口 9080）
│   └── docker/
│       ├── docker-compose.yaml
│       ├── backend/         # FastAPI：main.py / proxy.py / docker_scan.py / db.py
│       └── frontend/        # 原生 HTML+CSS+JS，无构建步骤
├── config/{privilege,resource}
└── cmd/                     # 生命周期脚本（状态上报、容器名清理）
```

## 内嵌浏览的实现要点

- 反向代理路径：`/p/{app_id}/...`，代理层会重写 `Location`、`Set-Cookie` 中的域名，并注入前端运行时补丁（劫持 fetch / XHR / WebSocket / EventSource / history），把站点发起的绝对路径请求补上代理前缀。
- 因此对 OpenList、Emby、Cloudreve、qBittorrent 等常见面板可直接内嵌登录并操作。
- 少数站点用 `X-Frame-Options` / CSP 强限制或被代理识别，内嵌会失败 → 工具条上点「直连打开」在新标签页访问。

## 非应用中心方式运行（调试用）

```bash
cd app/docker
docker compose up -d --build
# 浏览器访问 http://<NAS地址>:9080/
```

## 注意事项

- 容器使用 `network_mode: host`，容器内可直接探测 `127.0.0.1:<port>`，因此扫描结果与宿主端口一致；这也意味着 9080 必须是空闲端口。
- 只读挂载 `docker.sock`，应用仅做 `docker ps` 类查询，不会启停你的容器。
- 数据库为单文件 SQLite，备份 `apphub.db` 即备份全部访问列表。
- 应用本身无账号体系，依赖飞牛应用中心入口的访问控制；如需外网访问请在飞牛「远程访问/反向代理」侧加认证。
