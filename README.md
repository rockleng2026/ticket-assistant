# gpiao — 演出票自动抢购助手

用 ADB + uiautomator2 驱动安卓手机，在**开售时刻自动点击**购票按钮，
把「人肉掐表抢票」变成「定时任务」。

已用真实场景验证：**美团 App → 演唱会 → 石凯「时空乐园」2026巡回演唱会-深圳站**
（2026.11.28 19:00，深圳市宝安体育中心体育馆，票价 480-1180）。

---

## 1. 思路

### 1.1 为什么不推荐「电脑端 + 安卓虚拟机」

网上常见的方案是雷电/MuMu 模拟器 + 脚本。实测下来问题不少：

| 问题 | 说明 |
|---|---|
| 登录态 | 模拟器是全新 App 实例，**要重新走一遍短信验证**；且美团对模拟器有风控 |
| 实名信息 | 演唱会是**实名制**的，观演人姓名/证件号必须提前在 App 里填好 |
| 支付 | 模拟器里绑不了你常用的免密支付，付款这一步必然要人工介入 |
| 稳定性 | 部分风控/设备指纹场景下模拟器与真机表现不一致 |

所以本项目把**模拟器当开发调试环境**，**真机当生产环境**——两者走完全相同的代码，
只在配方里改一行 `device.connect` 就切换。

### 1.2 核心设计

```
   电脑(PC)                          手机(Android)
┌──────────────┐  adb over USB/WiFi  ┌────────────────────────┐
│  gpiao 进程  │ ───────────────────> │  美团 App (真实登录态)  │
│              │      uiautomator2    │                        │
│  · 配方      │ <─────────────────── │  · 演出详情页           │
│  · 选择器    │   截图 / UI dump     │  · 购票面板             │
│  · 定时器    │                     │  · 订单页               │
└──────────────┘                     └────────────────────────┘
```

四条关键工程决策：

**① 不用死坐标，用「多候选 + 降级」定位控件**

每个按钮配一串候选定位条件，按优先级尝试：

```yaml
buy_button:
  candidates:
    - {text: 立即购票}
    - {text: 立即购买}
    - {text: 去抢票}
    - {textContains: 购票}
    - {descContains: 立即购票}
    - {coord: [0.537, 0.959]}   # 最后兜底：按屏幕比例
  aliases: [立即购票, 立即购买, 去抢票, 预约]
```

`aliases` 解决的是**文案会变**的问题：开售前按钮是「预约」/「即将开售」，
开售后变成「去抢票」/「立即购票」，同一个 target 全程通用。

坐标一律存**屏幕比例**而不是绝对像素，换机型不会错位。

> 实测坑点：美团首页金刚区（电影演出/团购/外卖…）的图标文字**不是 text 而是
> content-desc**，只按 text 查会永远找不到。所以选择器必须同时支持
> `text` 和 `descContains` 两套关键字。

**② 开售前把能做的都做完**

网络往返、弹窗、选场次、选票档、选数量——这些在 T-0 之前全部做完，
页面停在「立即购票」按钮上。到点那一刻只剩**一次点击**。

**③ 到点瞬间走「零查询」路径**

到点后每次 `uiautomator` 查询要 150-300ms，这段时间票早没了。
所以标定时会把按钮坐标一起存下来，到点直接按坐标点（`mode: coord`），
零查询延迟；同时低频（400ms 一次）检查是否已跳转到购票面板，一跳转到就立刻停手，
避免重复下单。

**④ 计时用 monotonic + 忙等，最后 40ms 不睡**

```python
deadline = time.monotonic() + (target_epoch - time.time())
# 粗睡到距目标 40ms → 忙等到点
```

系统时间被 NTP 调整也不影响（monotonic 不跳变），唤醒误差实测 < 2ms。

---

## 2. 安装

```bash
cd D:\project\goupiao
python -m pip install -r requirements.txt
```

需要环境里有：

- **Python 3.9+**（本机 3.12）
- **Android Platform Tools**（adb，本机 `D:\Android\SDK\platform-tools`）
- 手机上开启 **USB 调试** 或 **无线调试**

体检：

```bash
python -m gpiao doctor
```

---

## 3. 接入手机

### 3.1 USB 数据线（推荐，最稳）

1. 数据线插电脑，手机下拉通知栏把 USB 用途改成 **传输文件 / MTP**
2. 手机上「开发者选项 → USB 调试」打开，弹出授权框点**允许**
3. 电脑上：

```bash
python -m gpiao doctor        # 应该能看到你的手机
```

### 3.2 无线 ADB（充电时也能用）

```bash
# 手机：设置 → 关于手机 → 连点「版本号」7 次 → 开发者选项 → 无线调试
python -m gpiao connect --host 192.168.0.23 --port 39871 --pair   # 首次配对，输入 6 位配对码
python -m gpiao connect --host 192.168.0.23 --port 39871          # 正式连接
```

> 配对端口和调试端口是**两个不同的数字**，每次重开无线调试调试端口都会变。

USB 已连上时也可以转无线：

```bash
adb tcpip 5555
python -m gpiao connect --host <手机IP> --port 5555
```

---

## 4. 标定：把控件位置录进配方

美团每次发版 UI 都会微调，坐标/文案随时可能变。所以**不要一次写死**，用录制器标定：

```bash
# 1) 手机停在要标定的页面
python -m gpiao inspect                     # 列出当前页所有可定位节点
python -m gpiao record -r recipes/shikai-shenzhen.yaml -t buy_button
#    → 打印节点列表，输入编号，例如 12
#    → 想同时兼容多种文案可以输入：12,立即购票,去抢票,马上抢
```

录制器会把节点的 `text` / `content-desc` / `resource-id` / 屏幕比例坐标
一起写进配方。

---

## 5. 抢票任务（先建任务，到点再续跑）

票是提前几天就公布的，开售时间也就提前几天知道。所以**不用等到临近才写脚本**——
建一个任务，把流程备好，开抢前 1 小时回来执行就行。

```bash
# 1) 现在就把任务建好（可重复执行，加 --force 覆盖）
python -m gpiao task create \
  --id panweibo-madlove-sz \
  --platform meituan \
  --name "潘玮柏MADLOVE:ULTRA巡回演唱会-深圳站" \
  --keyword 潘玮柏 --city 深圳 \
  --price 680 --qty 1 --attendees 1 --attendees-filled \
  --at "2026-11-27 13:22:00" \
  --prepare-lead 60 --lead 0.5

# 2) 看有哪些任务
python -m gpiao task list
python -m gpiao task show panweibo-madlove-sz

# 3) 开抢前 1 小时回来执行（会自动检查时间：没到就提示你等）
python -m gpiao task resume panweibo-madlove-sz

#    还没到准备时间？让它原地等：
python -m gpiao task resume panweibo-madlove-sz --wait-now

#    只做准备不抢购（提前验证控件标定对不对）：
python -m gpiao task resume panweibo-madlove-sz --arm-only

#    立刻演练一次（不等时间）：
python -m gpiao task run panweibo-madlove-sz --skip-wait
```

### 任务 vs 配方

| | 任务 `tasks/<id>.json` | 配方 `recipes/<id>.yaml` |
|---|---|---|
| 存什么 | 目标 + 参数 + 时间表 + 状态 | 怎么点（选择器 + 步骤） |
| 谁生成 | `task create` 按平台模板生成 | 同上 |
| 谁改 | `task create --force` 或改参数后 resume | `gpiao record` 标定控件 |

`task resume` 会先把任务里的参数（价格优先级、数量、观演人数、开售时间）
**同步回配方**，再执行。所以改价格只需要改任务，不用动配方。

### 任务状态

`created` → `armed`（设备已摆到购买页）→ `fired`（已开抢）→ `done`

### 平台

| 平台 | 状态 | 说明 |
|---|---|---|
| `meituan` | **已完整标定** | 美团 App 猫眼演出，实测可用 |
| `damai` | **脚手架** | 大麦 App（`cn.damai`）。控件需要用 `gpiao record` 标定一次 |

大麦接入步骤：

```bash
# 1) 建一个Damai任务
python -m gpiao task create --platform damai --keyword 演出名 --price 680 --qty 1 \
    --at "2026-12-05 19:30:00"
# 2) 手机上打开大麦到演出详情页，逐个标定控件
python -m gpiao record -r recipes/<id>.yaml -t search_entry
python -m gpiao record -r recipes/<id>.yaml -t buy_button
# 3) 用 arm-only 验证整条路径
python -m gpiao task resume <id> --arm-only
```

---

## 6. 每次开跑前的参数问询

抢票的三件事必须每次确认，`gpiao run` / `gpiao arm` 会先问你：

```
========================================================
 抢票参数确认
========================================================
1) 可接受的价格优先级（从高到低，逗号分隔） [¥680, ¥480, ¥880]: 680-480-880
2) 购票数量 [2]: 2
3) 实名观演人已在美团 App 里预填写好了吗？(y/n) [y]: y

→ 本次策略：
   价格优先级 : ¥680 > ¥480 > ¥880
   数量       : 2 张
   观演人     : 已预填
```

* **价格**是**优先级清单**不是单一价格：先点 `680`，这一档没票就立刻换 `480`，
  再没有就换 `880`，每次都重新走一遍确认下单流程
* **观演人没预填**是致命项（演唱会是实名制，位数必须等于票数，
  否则「立即支付」点不动）。选 `n` 时脚本会按 1 位处理并提示你去 App 里补
* 答案会写回配方，保证策略可复现
* 加 `--yes` 可跳过问询，直接用配方里保存的值

---

## 7. 配方（Recipe）

一份配方 = 一场演出的完整抢票策略。`recipes/shikai-shenzhen.yaml`：

```yaml
name: 石凯「时空乐园」2026巡回演唱会-深圳站
concert:
  search_keyword: 石凯
  session: 深圳站
  quantity: 2
sale:
  at: "2026-11-24 12:14:00"   # 开售时间
  lead_seconds: 1.2            # 提前量
  burst_seconds: 8.0           # 连点窗口
  tap_interval_ms: 180         # 连点间隔
device:
  connect: 192.168.0.23:39871  # 换成真机 / 模拟器只改这一行
```

### 步骤的三个阶段

| phase | 时机 | 做什么 |
|---|---|---|
| `pre` | 开售前 | 启动 App → 进演出页 → 选场次/票档/数量 → 停在购买按钮 |
| `fire` | 到点瞬间 | 连点购买按钮，检测到跳转就停 |
| `post` | 抢到后 | 提交订单（**绝不碰支付**） |

可用的 action：

```
launch      启动 App（默认先 stop 再 start，保证状态干净）
deep_link   打开 deeplink
search      搜索框输入并回车
search_submit  搜索（更稳：确认输入框拿到焦点后再打字）
tap         点击命名控件
tap_text    点击字面文本
wait / wait_text / wait_gone   等待
scroll_to / scroll_until        滚动直到目标出现
scroll_top  滚回页面顶部（详情页滚动会带偏底部按钮坐标，必须先归位）
back        返回
select      选择（命名控件或字面文本）
select_tier 按价格优先级逐档尝试（680 → 480 → 880），带游标
select_attendees  校验/补选实名观演人（快路径只查一次）
select_quantity  点「+」加数量
loop        重复执行一组步骤直到条件满足（换档重试整个下单流程用）
dismiss     处理开售公告/实名制须知/登录引导等弹层
              rules: [{text, expand:[偏移], trigger, timeout, optional},
                      {back_when: "登录体验完整功能"}]
require     前置断言（expect: present / absent）
burst       连点（核心动作）
              target, until, mode(auto|query|coord), window, interval_ms,
              check_every_ms, blockers[]
stop        停止并留证
dump        导出 UI 层级
```

### 选择器候选类型

```yaml
candidates:
  - text: 立即购票            # 精确文本
  - textContains: 购票
  - descContains: 电影演出     # content-desc（美团金刚区图标只有这个）
  - resourceId: com.sankuai.meituan:id/xxx
  - xpath: "//*[@text='立即购票']"
  - coord: [0.5585, 0.9414]    # 屏幕比例坐标（最后兜底）
  - bottomButton: true         # 屏幕最底部的按钮（应对动态文案）
```

### 两个「真花钱」开关

| 开关 | 默认 | 作用 |
|---|---|---|
| `submit_order` | false | 允许点「立即支付」**生成订单**（锁票 15 分钟） |
| `auto_pay` | false | 允许点支付页的最终按钮**真的扣钱** |

步骤上用 `requires_submit: true` / `requires_pay: true` 分别挂这两个开关。

---

## 8. 命令

```bash
python -m gpiao doctor                          # 环境体检
python -m gpiao connect --host <IP> --port <P>   # 真机接入
python -m gpiao inspect                         # 列出当前页可定位节点
python -m gpiao record -r <recipe> -t <名字>     # 标定控件
python -m gpiao arm    -r <recipe>               # 只做准备阶段
python -m gpiao run    -r <recipe>               # 准备 + 等待 + 到点抢
python -m gpiao run    -r <recipe> --skip-wait   # 不等待，立即演练
python -m gpiao fire   -r <recipe>               # 设备已就位，直接开抢
python -m gpiao shot   -o out.png                # 截图
python -m gpiao dump   -o ui.xml                 # 导出 UI 层级
python -m gpiao tap --text "立即购票"            # 临时点一下
python -m gpiao swipe --x1 540 --y1 1200 --x2 540 --y2 700

# 任务
python -m gpiao task create --keyword 演出名 --price 680 --qty 1 --at "..."
python -m gpiao task list
python -m gpiao task show <id>
python -m gpiao task resume <id>                 # 到点了继续跑
python -m gpiao task resume <id> --arm-only      # 只准备
python -m gpiao task resume <id> --wait-now      # 原地等到准备时间
python -m gpiao task rm <id>
```

每次运行都会在 `runs/<时间戳>-<阶段>/` 留下**截图 + UI dump + 事件流水**，
失败时可回溯。

---

## 9. 实测记录：石凯深圳站

> 全部数据来自真机（realme RMX5090 / Android 16 / 1264×2780 / USB ADB）。

### 7.1 真实路径（已逐段跑通并全链路验证）

```
美团首页
  └ [content-desc="电影演出"]           金刚区，注意是 desc 不是 text
     └ [text="演唱会"]                    猫眼演出频道顶部 tab
        └ [text="搜索明星 演出 赛事 厂牌 场馆"]   搜索框
           └ 输入「石凯」→ 回车
              └ [text="石凯「时空乐园」2026巡回演唱会"]
                 └ 演出详情页
                    ├ 深圳站 / 11.28 / 热卖 / ￥480-1180
                    ├ 「抢票攻略：预填信息，抢票快人一步」← 强烈建议提前填
                    └ [text="立即购票"]       底部固定条
                       └ 弹层①「四次开售公告」需**先点末尾的「展开」箭头**才能点「我已阅读并同意」
                          弹层②「实名制购票须知」→「知道了」
                          └ 「选择票档」面板
                              ├ 场次 2026-11-28 周六 19:00
                              ├ 票档 ¥1180 / ¥880 / ¥680 / ¥480
                              ├ 数量 1张 →（点 +）→ 2张，每单限购 4 张
                              └ 总计 ￥1760 → 「确认」→ 订单页 → 支付
```

### 7.2 实测结果

| 环节 | 结果 |
|---|---|
| 开售前准备（冷启动→演出详情页→清弹层→回顶→确认按钮在位） | **全程无人工干预，约 100s** |
| 到点触发 → 「选择票档」面板弹出 | **0.83s** |
| 面板弹出 → **票档锁定**（`¥680` 选中，「数量」行出现） | **2.05s** |
| 票档 → 数量 → 「确认」→ 订单页 | 合计 **3.7s** |
| 订单页 → 「立即支付」→ 支付页 | **2.25s** |
| **整条热路径（burst → 支付页）** | **6.35s** |
| 订单生成 | 成功（`¥680 × 1`，观演人校验通过，支付页 ¥479.55） |
| 真实付款 | **不自动化**，停在支付页由你手动确认 |

三次独立的完整计时演练（`gpiao run`，开售时间设为未来 130~170 秒）：

| 次 | 触发偏差 | 面板弹出 | 选票档 | 数量 |
|---|---|---|---|---|
| 1 | **-6.5ms** | 0.87s | ✓ | 2 张 |
| 2 | **-10.4ms** | 0.82s | ✓ | 2 张 |
| 3 | **-12.8ms** | 0.80s | ✓ | 2 张 |

最终停在：

```
数量 每单限购 4 张   [−] 2张 [+]      总计: ￥1760   [ 确认 ]
```

### 7.3 性能实测（真机）

| 操作 | 延迟 | 说明 |
|---|---|---|
| `d(text=...)` 选择器查询 | **45ms**（29–71） | 走无障碍服务，比整树 dump 快得多 |
| `dump_hierarchy()` 整树导出 | **148ms**（89–198） | 只在取证/标定时用 |
| `d.click()` → atx-agent JSON-RPC | **167ms** | HTTP 往返开销 |
| `adb shell input tap` | **70ms** | **最快**，到点连点走这条通道 |

> 真机上 `adb shell input tap` 比 atx-agent 的 JSON-RPC 快 2.4 倍，
> 所以 `Device.tap()` 默认走 adb 通道；`benchmark_tap()` 可现场比一次再定。

### 7.4 计时精度

`gpiao/clock.py` 的 `sleep_until` 用 `time.monotonic()` 倒计时 + 最后 40ms 忙等，
真机三次完整演练的到点偏差为 **-6.5ms / -10.4ms / -12.8ms**（负=稍晚）。

> 实现要点：`count_down()` 一直等到 `fire_epoch`（开售时间 - 提前量），
> `fire_now()` 再做精唤醒。**两者不能各等一次**——早期版本重复等待，
> 导致每次白白迟到一个 `lead_seconds`（实测 1.5s），已修。

设备时钟与电脑时钟偏差在 `doctor` 里会打印，超过 0.5s 需要先对时。

### 7.5 踩到的坑（都已在框架里处理）

| 坑 | 现象 | 处理 |
|---|---|---|
| 金刚区用 desc | 按 text 永远找不到「电影演出」 | 选择器支持 `descContains` |
| 按钮文案带空格 | 底部按钮是「已 预 约」，`text="预约"` 匹配不到 | 新增 `textNorm` 候选（XPath `normalize-space`） |
| 公告弹层按钮禁用 | 灰色「我已阅读并同意」点不动 | `dismiss` 的 `expand` 先点末尾的「展开」箭头（该箭头是画出来的图标，无无障碍节点，靠几何定位） |
| 弹层出现时机不定 | 20s ~ 数分钟后才冒出来 | `dismiss` 给足超时；burst 阶段还有 `blockers` 兜底 |
| 按钮文案随状态变 | 「预约」→「去抢票」→「立即购票」 | `aliases` 机制 |
| 详情页会滚动 | 返回后滚动位置残留，按钮坐标偏移，点到了「声入人心」 | 准备阶段 `scroll_top` 回顶 |
| 票档面板有上滑动画 | 动画没结束就点，表现为「点了没反应」，极易误判成没票 | `select_tier` 等位置稳定再点，点空补点一次 |
| 数量 +/- 无文本 | 是 ImageView，text/desc 都是空 | 只能用屏幕比例坐标定位 |
| 登录半屏弹层挡住首页 | 关不掉导致找不到入口 | `dismiss` 支持 `back_when` |
| App 状态不确定 | 上次停在登录页 | `launch` 默认 stop + start |
| uiautomator2 3.x 的 bounds 是 dict | 老解析取不到坐标 | `_to_bounds` 兼容 dict/列表/字符串三种格式 |
| 支付页主按钮文案动态 | 「已减0.45元，月付交易」 | 新增 `bottomButton` 候选（屏幕最底那个按钮） |

---

## 10. 上手建议（按这个顺序）

1. **提前 1 天**：App 里先登录好、**填好实名观演人信息**、绑定支付方式
   （详情页的「抢票攻略：预填信息，抢票快人一步」→「去填写」）
2. **提前 1 小时**：连接手机，跑 `python -m gpiao arm -r <recipe>` 验证整条前置路径通
   （每一步都截图了，通了就说明控件标定是对的）
3. **提前 10 分钟**：跑 `python -m gpiao fire -r <recipe>` 演练一次连点
   （设备已就位时直接开抢；**先在不太重要的场次上练**）
4. **开售前 1 分钟**：跑 `python -m gpiao run -r <recipe>`，先回答三个参数问题，
   然后让它自己等到点
5. **抢到后**：脚本停在支付页，**你手动点支付**（订单锁 15 分钟）

### 怎么知道开售时间

- 演出详情页的「四次开售公告」里写着，例如
  `本场演出将于2026年11月24日12：14在官方售票平台第四次开售`
- 搜索结果里，深圳站显示「去抢票」= **正在开售**；显示「预约」= **还没开售**
- 把时间填进配方的 `sale.at`：

```yaml
sale:
  at: "2026-11-24 12:14:00"
  lead_seconds: 0.5     # 提前量，见下
```

### lead_seconds 怎么调

服务器到点才放票，客户端请求要**略微提前**发出去排队：

- `0.3` ~ `0.8`：抢热票（几秒内售罄）时更激进
- `1.2`（默认）：稳妥，票源充足时无所谓
- 突发开售（临时加场）建议 `0.3`，因为公告往往是临时发的

burst 会连点 `burst_seconds` 秒（默认 8s），命中「已进入选票面板」就立刻停，
不会重复下单。

### 抢票时间线（实测）

```
T-1.2s   脚本开始连点「立即购票」
T+0.83s  「选择票档」面板弹出
T+2.05s  票档已锁定（¥680 选中）        ← 票到手的关键点
T+3.7s   「确认」已点，订单页就绪
T+6.35s  「立即支付」已点，支付页就绪    ← 你手动付
```

---

## 11. 边界与注意事项

- 本项目只做**你本人账号**的自动化操作，不碰验证码识别、不做多账号并发
- **付款不自动化**：`auto_pay: false`，脚本停在支付页，你自己点支付
- **实名观演人必须提前在 App 里填好**，位数必须等于票数，否则「立即支付」点不动
  （脚本会在订单页做一次快校验，只花 ~45ms）
- 美团 App 每次更新都可能改文案，控件标定要跟着复核（`arm` 一跑就知道）
- 票档/数量请在配方里写死，脚本不会选错档
- `atx-agent` 会装到你手机上（uiautomator2 的设备端助手），
  用完可 `adb shell pm uninstall com.github.uiautomator` 卸载

---

## 12. 目录

```
gpiao/
  cli.py          命令行入口
  config.py       配方模型（配置/时间/设备）
  task.py         任务模型（目标/参数/时间表/状态/持久化）
  platforms.py    平台模板（meituan 已标定 / damai 脚手架）
  device.py       设备层（连接/准备/快速点击通道/截图）
  selectors.py    控件选择器（多候选 + 降级 + 坐标兜底 + 底部按钮）
  flow.py         流程引擎（action 分发 + burst 连点 + 换档重试 + 弹层处理）
  clock.py        精确计时（monotonic + 忙等）
  ask.py          开跑前的参数问询（价格 / 数量 / 观演人）
  logging_setup.py 日志与证据留存
recipes/          演出配方
tasks/            抢票任务
tests/            单元测试（34 个，不依赖真机）
runs/             每次运行的截图/dump/事件日志
```
