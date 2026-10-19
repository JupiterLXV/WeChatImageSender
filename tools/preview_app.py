"""Capture only the sender's demo window; never connects to WeChat."""
import json
import argparse
import sys
import tkinter as tk
from pathlib import Path

from PIL import ImageGrab

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from sender.core import scan_folder
from sender.gui import Application
from sender.winutil import dpi_aware

dpi_aware()
parser = argparse.ArgumentParser()
parser.add_argument('--real-layout', action='store_true', help='Show consent UI; only runs the read-only version check')
args = parser.parse_args()
root = tk.Tk()
root.geometry('+30+30')
app = Application(root, demo=not args.real_layout)
app.folder.set(str(PROJECT / 'test_assets'))
app.photos = scan_folder(PROJECT / 'test_assets', False, True)
app.update_tree()
root.lift()
root.attributes('-topmost', True)


def capture():
    root.update_idletasks()
    x, y = root.winfo_rootx(), root.winfo_rooty()
    width, height = root.winfo_width(), root.winfo_height()
    output = PROJECT / 'data' / ('app-preview-live.png' if args.real_layout else 'app-preview.png')
    ImageGrab.grab(bbox=(x, y, x + width, y + height)).save(output)
    root.attributes('-topmost', False)
    print(json.dumps({'window': [width, height],
                      'screen': [root.winfo_screenwidth(), root.winfo_screenheight()],
                      'actions_visible': bool(app.actions.winfo_viewable()),
                      'log_visible': bool(app.log.winfo_viewable()),
                      'screenshot': str(output)}))
    root.destroy()


root.after(1500, capture)
root.mainloop()
