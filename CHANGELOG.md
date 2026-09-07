# Changelog

## 1.4.1
- improvements

## 1.4.0
- Complete rewrite in Python, now available as a PyPI CLI package
- Added new commands (`format-cache`, `format-storage`, `format-data-storage`, `format-frp`, `wipe-data-storage`, `wipe-efs`)
- Added reading for new device details (`Mi Token`, `Carrier`, `Recovery Version`)
- General improvements and bug fixes

## 1.3.0
- Fixed Windows crashes and data corruption when flashing large ROMs (#7, PR #10, thanks @MaChInEgUn3)
- Windows release is now a zip with the .exe and its required DLLs, instead of a bare .exe (#5)
- Dropped the 32-bit Windows build

## 1.1.0
- Added the option to flash other suggested ROM versions, not just the currently installed one, via "ROMs that can be flashed"
- Minor fixes and improvements