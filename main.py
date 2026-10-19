import argparse
import json
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox


def main():
    parser = argparse.ArgumentParser(description='微信图片顺序发送助手')
    parser.add_argument('--demo', action='store_true', help='演示队列，不连接微信')
    parser.add_argument('--diagnose', action='store_true', help='只读检测微信界面，不发送消息')
    parser.add_argument('--diagnose-output', type=Path, help='将只读诊断结果保存为 JSON 文件')
    parser.add_argument('--smoke-test', action='store_true', help='检查界面创建后自动退出')
    args = parser.parse_args()
    from sender.winutil import dpi_aware, SingleInstance
    dpi_aware()
    if args.diagnose:
        from sender.wechat import WeChatBackend
        if sys.stdout:
            sys.stdout.reconfigure(encoding='utf-8')
        result = json.dumps(WeChatBackend().diagnose(), ensure_ascii=False, indent=2)
        if args.diagnose_output:
            args.diagnose_output.write_text(result, encoding='utf-8')
        if sys.stdout:
            print(result)
        return
    from sender.gui import Application
    root = tk.Tk()
    root.withdraw()
    try:
        instance = SingleInstance(demo=args.demo)
    except Exception as exc:
        messagebox.showinfo('微信图片顺序发送助手', str(exc), parent=root)
        root.destroy()
        return
    try:
        app = Application(root, demo=args.demo)
        root.deiconify()
        if args.smoke_test:
            root.after(800, root.destroy)
        root.mainloop()
    finally:
        from sender.accessibility import restore_all
        restore_all()
        instance.close()


if __name__ == '__main__':
    main()
