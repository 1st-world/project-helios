# Frontend assets

This inventory records the pinned third-party files served by the application. All runtime files are included locally; the browser does not fetch fonts, stylesheets, or scripts from external CDNs.

## Runtime files

Paths are relative to the project root. Copy the original files without changing their license headers or rebuilding the bundles.

| Resource | Version | Destination | Download |
| --- | --- | --- | --- |
| Pretendard Variable | 1.3.9 | `static/fonts/pretendard/1.3.9/PretendardVariable.woff2` | [WOFF2](https://raw.githubusercontent.com/orioncactus/pretendard/v1.3.9/packages/pretendard/dist/web/variable/woff2/PretendardVariable.woff2) |
| Marked | 18.0.14 | `static/vendor/marked/18.0.14/marked.umd.js` | [UMD bundle](https://cdn.jsdelivr.net/npm/marked@18.0.14/lib/marked.umd.js) |
| marked-highlight | 2.2.4 | `static/vendor/marked-highlight/2.2.4/index.umd.js` | [UMD bundle](https://cdn.jsdelivr.net/npm/marked-highlight@2.2.4/lib/index.umd.js) |
| DOMPurify | 3.2.4 | `static/vendor/dompurify/3.2.4/purify.min.js` | [Browser bundle](https://cdnjs.cloudflare.com/ajax/libs/dompurify/3.2.4/purify.min.js) |
| highlight.js | 11.11.1 | `static/vendor/highlight.js/11.11.1/highlight.min.js` | [Browser bundle](https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.11.1/highlight.min.js) |
| highlight.js GitHub Dark | 11.11.1 | `static/vendor/highlight.js/11.11.1/github-dark.min.css` | [Theme](https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.11.1/styles/github-dark.min.css) |
| Lucide | 1.48.0 | `static/vendor/lucide/1.48.0/lucide.min.js` | [UMD bundle](https://unpkg.com/lucide@1.48.0/dist/umd/lucide.min.js) |

## License files

Keep the complete original license beside each dependency's runtime files.

| Resource | Destination | Download |
| --- | --- | --- |
| Pretendard | `static/fonts/pretendard/1.3.9/LICENSE` | [License](https://raw.githubusercontent.com/orioncactus/pretendard/v1.3.9/LICENSE) |
| Marked | `static/vendor/marked/18.0.14/LICENSE` | [License](https://cdn.jsdelivr.net/npm/marked@18.0.14/LICENSE) |
| marked-highlight | `static/vendor/marked-highlight/2.2.4/LICENSE` | [License](https://cdn.jsdelivr.net/npm/marked-highlight@2.2.4/LICENSE) |
| DOMPurify | `static/vendor/dompurify/3.2.4/LICENSE` | [License](https://cdn.jsdelivr.net/npm/dompurify@3.2.4/LICENSE) |
| highlight.js | `static/vendor/highlight.js/11.11.1/LICENSE` | [License](https://cdn.jsdelivr.net/npm/highlight.js@11.11.1/LICENSE) |
| Lucide | `static/vendor/lucide/1.48.0/LICENSE` | [License](https://unpkg.com/lucide@1.48.0/LICENSE) |

## Typography

All UI text inherits `--font-sans`. Pretendard Variable is the preferred family, with system UI, Korean, and emoji fallbacks. Its local `@font-face` uses `font-display: swap` and the official weight range of `45 920`. The browser loads the WOFF2 file through CSS when needed, and the server registers `font/woff2` independently of host MIME defaults. The page does not preload the font because saved preferences may use another family.

Only `pre`, `code`, `kbd`, and `samp` use `--font-mono`. This token uses installed system monospace fonts and requires no separate font download. Usage figures and numeric inputs retain the UI family and use `font-variant-numeric: tabular-nums` where alignment is needed.

Settings > General immediately applies and saves the UI family as the user selects system fonts or enters a named installed font. The system choice uses the operating system's interface fonts rather than the browser's default document font. The preference is saved per browser origin in local storage; font files and discovered font lists are not uploaded or stored. Selecting Installed font automatically attempts discovery in supported secure browser contexts, preserving user activation for the permission request. Restoring a saved choice during page startup does not request permission. Manual family entry remains available, with Pretendard and system fallbacks when a selected font is unavailable or blocked. Selecting Pretendard restores the default, and code keeps the shared monospace token.

Text size immediately scales UI and code text through `--ui-font-scale`. Its preference is stored separately from the font family, restored on startup, and synchronized across tabs of the same origin. Icons retain their dimensions, and the composer adjusts its height when the text scale changes.

Successful changes and font discovery do not display separate notices. Feedback appears only while discovery is pending or when discovery, input validation, or preference storage needs attention.

## Updates

1. Review upstream changes and download the intended pinned versions with their licenses.
2. Verify font metadata and library versions, and record byte sizes and SHA-256 hashes in `manifest.json`.
3. Update versioned paths in this inventory, `static/style.css`, and `templates/index.html` together. Preserve the script order and load the highlight theme before the application stylesheet.
4. Verify font loading, Markdown, syntax highlighting, sanitization, and icons with external browser requests blocked.

`manifest.json` records the imported files' source URLs and hashes. The hashes detect local changes; they are not independent upstream signatures. Preserve the upstream files and their license headers without rebuilding or reformatting them.
