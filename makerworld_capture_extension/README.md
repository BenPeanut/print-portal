# MakerWorld Capture Extension (Standalone)

This is a standalone browser extension, separate from your website UI.

## What it does

- On MakerWorld: hover a model card and press `Q`.
- The extension captures the hovered model link and scrapes model metadata.
- The extension popup now hosts the same Desktop Capture app UI (embedded from your local app).
- Opening the popup loads the latest Q-captured model into that UI automatically.

## Load in Chrome (exact steps)

1. Open Chrome.
2. Go to `chrome://extensions`.
3. Turn on **Developer mode** (top-right toggle).
4. Click **Load unpacked**.
5. Select this folder:
	`C:\Users\Benaiah\Documents\MyCode\MyPrintingBuisness\Client_Website\makerworld_capture_extension`
6. Confirm the extension appears as **MakerWorld Capture Companion**.
7. Click the extension card's **Details** button and pin it from the puzzle icon menu so it is easy to open.

## After code changes (important)

1. Go back to `chrome://extensions`.
2. On **MakerWorld Capture Companion**, click **Reload**.
3. Refresh any open MakerWorld tabs so the updated content script is injected.

## Configure extension popup

- The popup uses your saved extension `API base` setting (default `http://127.0.0.1:5000`).
- Keep your local Flask app running so the embedded Desktop Capture UI can load.

## Usage

1. Make sure your Flask app is running.
2. Open MakerWorld in browser.
3. Hover a model card and press `Q`.
4. Click the extension icon.
5. The popup shows the full Desktop Capture UI and auto-loads the latest captured model.
6. Review/edit fields and click **Save Featured Item**.

## Notes

- This extension is independent from your website pages.
- You can remove or ignore the `model_capture_app` folder if you no longer want the in-site version.
