"""Run the native folder dialog in an independently terminable process."""

import json
import sys


def main() -> None:
    root = None
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(parent=root, title="Select Workspace Folder",
                                       initialdir=sys.argv[1] or None)
        result = {"status": "selected", "path": path} if path else {"status": "cancelled"}
    except Exception:
        result = {"status": "error", "detail": "Could not open folder picker. Enter the folder path manually."}
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
