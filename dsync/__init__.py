"""D-Sync — automatic sync daemon for external devices.

Overview:
  * Runs in the background (system tray) and watches for SD card insert/removal
  * Reads a YAML config from the card root and copies files to the destinations
  * Skips duplicate files automatically (decided by content hash)
  * Shows sync progress, logs and history on a local web dashboard
"""

__version__ = "1.1.0"
__all__ = ["__version__"]
