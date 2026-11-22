#!/bin/bash
# macOS：双击本文件启动 Credit Cookie 工具。
cd "$(dirname "$0")" || exit 1
python3 cookie_tool.py
status=$?
echo
if [ "$status" -ne 0 ]; then
  echo "cookie_tool.py 退出码：$status"
  read -r -p "按回车关闭窗口…" _
fi
