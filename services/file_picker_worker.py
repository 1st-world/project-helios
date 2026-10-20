"""Open a native multiple-file dialog on the Helios host and report its selection."""

import json
import sys


def main() -> None:
    """Select host files without reading them and release the native dialog resources."""
    root = None
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        paths = filedialog.askopenfilenames(
            parent=root,
            title="Select Files to Attach",
            initialdir=sys.argv[1] or None,
        )
        result = (
            {"status": "selected", "paths": list(paths)}
            if paths else {"status": "cancelled"}
        )
    except Exception:
        result = {
            "status": "error",
            "detail": "Could not open the file picker on the computer running Helios. Try Choose files again.",
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
