# 换一台机器继续开发

> 目标：把仓库拷到另一台 Windows（比如生产服务器）上，能接着改代码、出交付包，
> 并且新开的 WorkBuddy 任务**自动带上全部历史约定**。

## 一、先决条件

| 用途 | 需要 | 说明 |
|---|---|---|
| 装/验证平台 | **什么都不用装** | 直接跑 `platform/deploy/dist/` 出的完整离线包 |
| 改前端重新构建 | Node.js 18+ | `npm install` 会拉依赖（约 80 MB） |
| 重新打 exe | **Python 3.11（必须带 tkinter）** | PyInstaller 打包用，验收脚本锁 3.11–3.13 |
| 跑编排层 | Python 3.10+ | 服务器上一般用离线包自带的 py-orch |

自检 tkinter（打 exe 必需，精简版/conda/商店版常缺）：

```powershell
python -c "import tkinter; print('tkinter ok')"
```

## 二、拿到代码

```powershell
# 能连 GitHub 就用这条
git clone https://github.com/Xiucheng0623/zhitu-platform.git zhitu

# 连不上（国内服务器常见：clone 报 Empty reply from server / 443 超时）
# 用开发机导出的离线仓库包，3.4 MB，含完整历史
git clone D:\path\to\zhitu-platform.bundle zhitu
cd zhitu
git remote set-url origin https://github.com/Xiucheng0623/zhitu-platform.git
```

**放哪个目录都行**，不用和开发机路径一致 —— 项目记忆跟着仓库走，不跟路径绑定。

## 三、记忆是自动的，什么都不用做

项目记忆就在仓库里：`.workbuddy/memory/`

- `MEMORY.md` —— 精简后的长期约定（硬决策、端口、pg 踩的坑、前端同源构建……）
- `2026-11-05.md` ~ `2026-11-10.md` —— 每日工作日志，细节都在这里

WorkBuddy 在**这个目录下新建任务**时会自动读取它。第一次开口建议明确一句：

> 读 `.workbuddy/memory/MEMORY.md` 和 `2026-11-10.md`，这是智途项目的历史约定，读完再等我指令。

想追加记忆就让新任务正常写日志，`git diff` 里能看到，提交后带回开发机。

## 四、clone 之后的第一次自检（3 分钟）

```powershell
python platform\tools\check_package.py --source
```

期望最后一行 `源码树核对全部通过`。这一步不需要管理员、不需要联网、不需要数据库，
核的是「代码 / 脚本 / 迁移 / 断言齐不齐」，能立刻发现有没有搬残。

## 五、出交付包

```powershell
cd platform\web
npm install                 # 第一次要联网
npm run build               # → platform\web\dist

cd ..\deploy\windows-native
python make_payload.py                 # 收拢 dist/迁移/品牌 → payload/
python verify_payload.py               # 期望 60/60
python build_native.py --clean         # 两个 exe，约 1 分钟
python make_offline_release.py         # 完整离线包，约 6 分钟
```

`make_offline_release.py` 的暂存目录写死在 `D:/zhitu-offline-build`（约 882 MB：
pgsql / py-ls / py-orch / caddy）。**这个目录不在仓库里**：

- 图省事：把开发机上的 `D:\zhitu-offline-build\runtime` 整个拷过去；
- 或者改脚本里的 `OFFLINE` 常量指向新位置，让它自己重新准备（约 40 分钟）。

## 六、历史会话要不要搬

WorkBuddy 的会话是本地文件：`~/.workbuddy/projects/<工作目录slug>/<session-id>.jsonl`，
slug 由项目绝对路径生成，换机器就变了。

**不建议搬旧会话**：开发机上那个已经 95 MB，里面全是 `C:\Users\G\...` 的绝对路径，
在新机器上执行会一直失败。记忆文件才是有效上下文，读完就够。
