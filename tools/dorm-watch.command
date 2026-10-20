#!/bin/zsh
# 双击这个文件 = 开一个终端窗口看宿舍机干活（真的 tmux attach，能敲键盘）。
# **关掉窗口只是「不看了」** —— 活在那台的 tmux 里跑，不在这个窗口里，照跑不误。
# 想只读地看（不会误敲键盘）就用 `dorm view` 那个网页版。
exec "$HOME/bin/dorm" watch
