"""Run a native folder dialog in a separate process and report its result as JSON."""

import json
import sys


def main() -> None:
    """Open the native folder dialog, release Tk resources, and print one JSON result."""
    root = None
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(
            parent=root,
            title="Select Workspace Folder",
            initialdir=sys.argv[1] or None,
        )
        result = (
            {"status": "selected", "path": path}
            if path
            else {"status": "cancelled"}
        )
    except Exception:
        result = {
            "status": "error",
            "detail": "Could not open folder picker. Enter the folder path manually.",
        }
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
