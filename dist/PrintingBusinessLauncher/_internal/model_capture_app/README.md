# Model Capture Companion App

This folder contains a standalone browser-based companion app that is linked into the main Flask project.

## Route

- Open `/model-capture-app` after logging in as a normal user.

## What it does

- Provides a settings-first interface for default profile, filament, quantity, and post-confirm behavior.
- Generates a bookmarklet bridge script.
- On MakerWorld, after running the bookmarklet:
	- Hover a model card.
	- Press `Q`.
	- The app captures the hovered link and scrapes model metadata.
- Back in the companion app, it prepares a suggested order for confirmation.

## Notes

- This uses a secure capture token so it works even when MakerWorld cannot send localhost cookies.
- Confirmed captures are added to cart as `In Cart` orders in the main database.
