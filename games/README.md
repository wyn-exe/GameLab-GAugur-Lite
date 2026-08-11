# 八个真实小游戏

本目录保存 GAugur-Lite 正式实验使用的八个真实、可玩的 Pyxel 小游戏。它们来自 Pyxel 官方仓库的随附 examples/apps，不是本项目生成的合成 workload，也不是用资源 profile 冒充的游戏。

## 游戏清单

| 实验 ID | 游戏 | 上游入口 | 类型 |
| --- | --- | --- | --- |
| `pyxel_jump` | Pyxel Jump | `pyxel/02_jump_game.py` | 跳跃/躲避 |
| `pyxel_bubbles` | Pyxel Bubbles | `pyxel/06_click_game.py` | 鼠标点击 |
| `pyxel_snake` | Snake! | `pyxel/07_snake.py` | 贪吃蛇 |
| `pyxel_shooter` | Pyxel Shooter | `pyxel/09_shooter.py` | 纵版射击 |
| `pyxel_platformer` | Pyxel Platformer | `pyxel/10_platformer.py` | 横版平台 |
| `daylight` | 30 Seconds of Daylight | `pyxel/apps/30sec_of_daylight.pyxapp` | Roguelike |
| `mega_wing` | Mega Wing | `pyxel/apps/mega_wing.pyxapp` | 弹幕射击 |
| `space_rescue` | Space Rescue | `pyxel/apps/space_rescue.pyxapp` | 单键救援 |

## 直接试玩

要求 Python 3.11+ 和 Pyxel 2.9.8：

```powershell
python -m pip install pyxel==2.9.8

Push-Location games\pyxel
python 02_jump_game.py
# 或：python 06_click_game.py
# 或：python 07_snake.py
# 或：python 09_shooter.py
# 或：python 10_platformer.py
Pop-Location

pyxel play games\pyxel\apps\30sec_of_daylight.pyxapp
pyxel play games\pyxel\apps\mega_wing.pyxapp
pyxel play games\pyxel\apps\space_rescue.pyxapp
```

每次只运行一条试玩命令，按 `Esc` 退出。正式实验不通过操作系统级键鼠注入控制游戏，而由后续 `gaugur_lite` Pyxel 适配器在引擎 API 层提供固定 seed 的输入轨迹、采集 update/draw 帧时间并按计划退出。

## 目录说明

- `pyxel/apps/`：上游原始 `.pyxapp`，可以直接通过 `pyxel play` 运行；
- `pyxel/apps-src/`：从相应 `.pyxapp` 原样解包的源码和资源，供实验适配器加载；
- `pyxel/assets/`：五个脚本游戏所需的上游资源；
- `pyxel/LICENSE`：Pyxel 上游 MIT License；
- `pyxel/UPSTREAM.md`：来源 commit、选择范围和许可证说明；
- `pyxel/SHA256SUMS.txt`：原始入口、资源和 app bundle 的校验值。

这些文件保留第三方作者身份。对实验控制、遥测和自动输入的实现应放在 `gaugur_lite/`，不要直接修改本目录中的上游副本；若确需修补，必须单独记录 patch、原因和新校验值。

