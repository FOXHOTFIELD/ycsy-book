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

这些是写死的，配置文件改不动：

| 限制 | 值 |
|---|---|
| 同时存在的预约 | 最多 **3 条** |
| 单次运行时长 | 最长 **4 小时**，到点自动退出 |
| 铺满整天 | **不支持**，`--keep` 只接受 1–3 |
| 跨天预约 | **不支持**，只约当天还没开始的时段 |
| 单轮下单数 | `max_book_per_pass`，默认不限 |

界面不再显示剩余次数：值守每轮自己检查「还有没有次数、还有没有空台位」，
不合适就自动换到下一个实验。

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

### 手动指定实验室 / 实验

界面不提供这一项，直接写文件：

```json
// %LOCALAPPDATA%\ycsy-book\override.json   （Linux/macOS: ~/.local/share/ycsy-book/）
{ "lab_id": 12, "experiments": [206, 114] }
```

| 写法 | 效果 |
|---|---|
| 只写 `experiments` | 只用这几个实验，按书写顺序尝试 |
| 只写 `lab_id` | 只用这个实验室里的实验 |
| 两者都写 | 先用 `experiments`，`lab_id` 不再生效 |
| 删掉文件 | 回到自动发现 |

运行中改文件也有效（下一轮就会重新读取）。某个实验次数用尽或台位被占满时，
值守会自动换到列表里的下一个实验——这不需要你干预。

## 安全设计

- 账号密码只存本地，除平台外不发送给任何地方
- 同时只允许一个进程下单（跨进程文件锁，进程崩溃由系统自动释放）
- 取消操作必须显式指定（`--ids` / `--all` / 界面里逐个勾选）
- 日志里的远程桌面地址会截断，不落完整凭据

## 注意事项

1. 平台接口未公开，字段和行为可能变化；本项目只做自己账号的自动化，不提供代约、代跑。
2. 预约次数是稀缺资源，先 `ycsy plan` 看清楚再真跑。
3. **本项目可能随时失效或停止更新，恕不另行通知**；请以实际能否跑通为准。
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
