# ycsy-book

给「远程实验平台」用的实验台预约工具：命令行 + 终端界面。滚动保持预约、到点自动打开远程客户端。

> 非官方项目，与平台方无关，仅供**本人账号自用**。

## 安装

**方式一：下载可执行文件（推荐，不需要 Python）**

到 [Releases](https://github.com/your-name/ycsy-book/releases) 下载 `ycsy.exe`，双击就用。
Windows 可能弹 SmartScreen，点「更多信息 → 仍要运行」。

**方式二：pipx（需要 Python 3.9+）**

```bash
pipx install git+https://github.com/your-name/ycsy-book
ycsy
```

**方式三：从源码跑**

```bash
git clone https://github.com/your-name/ycsy-book && cd ycsy-book
pip install -e . --no-build-isolation
python -m ycsy
```

## 配置

不用手写文件，两条路都行：

```bash
ycsy tui        # 第一次启动会直接问学号和密码，验证通过后自动保存
ycsy account    # 纯命令行方式，随时改
```

配置文件按 `--config` → `./config.json` → `<用户配置目录>/config.json` 顺序查找
（Windows 是 `%LOCALAPPDATA%\ycsy-book\config.json`）。
想直接改文件，就从示例复制：

```bash
cp config.example.json config.json    # Windows: copy config.example.json config.json
```

也可以完全不落盘，用环境变量：

```bash
export YCSY_USERNAME=...  YCSY_PASSWORD=...
```

## 使用

```bash
ycsy tui              # 终端界面（推荐）
ycsy login            # 校验账号
ycsy status           # 我的预约
ycsy plan             # 只读：还差哪些时段
ycsy run --dry-run    # 彩排
ycsy run --keep 2     # 值守：保持当前 + 下一格
ycsy start            # 立刻开始当前这一格
ycsy cancel --all     # 取消所有还没开始的预约
ycsy close            # 关掉远程客户端
ycsy stop             # 停掉后台值守
```

## 内置限制

这两条是写死的，配置文件改不动：

| 限制 | 值 |
|---|---|
| 同时存在的预约 | 最多 **3 条** |
| 单次运行时长 | 最长 **4 小时**，到点自动退出 |

另外两条是发布版的行为约束：

| 限制 | 值 |
|---|---|
| 铺满整天 | **不支持**，`--keep` 只接受 1–3 |
| 跨天预约 | **不支持**，只约当天还没开始的时段 |

### 界面操作

| 键 | 作用 |
|---|---|
| `h` `j` `k` `l` / `↑` `↓` `←` `→` | 在菜单里移动 |
| `空格` / `Enter` | 选中当前项 |
| `m` | 切换模式（保持 1/2/3 格）并保存 |
| `u` | 配置账号（界面里填，Esc 不做修改返回） |
| `q` | 退出（可选是否清理预约） |

`保持 N 格` 也可以写在配置里（`run.keep`，1–3）或命令行 `--keep N`。
**自动开始默认就是开的，不需要在配置里写。**

## 预约次数与自动选择

### 平台怎么算次数

每个实验在平台上有一个**总次数上限**（`limit_count`，`ycsy survey` 最后一列）。

- 已用次数 = 仍然有效的预约 + **已经过期的历史预约**，两者都占额度
- 次数用完的实验会被自动跳过，不管它在不在你的列表里
- 同一个时段，同一个实验不会被重复下单

### 自动选择顺序

每个待补时段，值守按下面的步骤挑实验：

1. 按你的 `experiments` 列表顺序（没配就按平台返回的顺序）
2. 逐个检查三件事：**次数够不够**、**实验室在这个时段开没开**、**这个时段是不是已经被同一实验占过**
3. 取第一个**还有空台位**的下单
4. 下单失败（台位被抢 / 接口报错）就换列表里的下一个实验，直到成功或试完

所以遇到“某个实验次数用尽”“这个实验室没空台位”，不需要你干预——它会自己换下一个。

### 平台自身的限制

- 同一时段只能占一个台位
- 只能约**还没开始**的时段；已经开始但没约上的，界面里显示为「过」并划掉

## 手动指定实验室 / 实验

有两个入口，优先级不同：

| 入口 | 位置 | 生效时间 | 适合 |
|---|---|---|---|
| 配置文件 | `config.json` 里的 `lab_id` / `experiments` | 启动时读一次 | 长期固定 |
| 覆盖文件 | `%LOCALAPPDATA%\ycsy-book\override.json` | 每轮重新读，值守不用重启 | 临时切换 |

两边的字段一样：

```json
{
  "lab_id": 12,
  "experiments": [206, 114]
}
```

| 写法 | 效果 |
|---|---|
| 只写 `experiments` | 只用这几个实验，按书写顺序尝试 |
| 只写 `lab_id` | 只用这个实验室里的实验 |
| 两个都写 | `experiments` 优先，`lab_id` 不生效 |
| 都不写 / 删掉文件 | 自动发现全部实验 |

优先级：`override.json` > `config.json` > 自动发现。

**怎么查 id：**

```bash
ycsy survey
#   #206   实验一 组合电路自动化设计入门   实验室 数电10代(12) 09:00-22:00  每次 20 分钟  可预约 100 次
```

`#` 后面是实验 id，实验室名后面的括号是 `lab_id`。

Linux/macOS 的 `override.json` 在 `~/.local/share/ycsy-book/`；Windows 就在配置目录旁边，
和 `config.json` 同一个文件夹。

## 安全设计

- 账号密码只存本地，除平台外不发送给任何地方
- 同时只允许一个进程下单（跨进程文件锁，进程崩溃由系统自动释放）
- 取消操作必须显式指定（`--ids` / `--all` / 界面里逐个勾选）
- 日志里的远程桌面地址会截断，不落完整凭据

## 注意事项

1. 平台接口未公开，字段和行为可能变化；本项目只做自己账号的自动化。
2. 预约次数是稀缺资源，先 `ycsy plan` 看清楚再真跑。
3. 本项目可能随时失效或停止更新，恕不另行通知；请以实际能否跑通为准。
4. 使用本工具产生的任何后果（含平台条款风险）由使用者自行承担。

## 开发

```bash
pip install -e ".[dev]"
pytest
ruff check src
```

结构：`planner.py` 是纯逻辑（无网络、可测试），`providers/` 是平台适配层，
`service.py` 负责编排，`cli.py` 和 `tui.py` 只是两个前端。

## License

MIT
