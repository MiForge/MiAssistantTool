import os
import platform
import shutil
import subprocess
import sys

import usb.backend.libusb1

from miassistant import core

_MARKER = os.path.expanduser("~/.miasst_deps_ok")


def _run(cmd):
    if not shutil.which(cmd[0]):
        return False
    print(f"Installing missing dependency: {' '.join(cmd)}")
    subprocess.run(cmd)
    return True


def _ensure_windows():
    if core._BACKEND is not None:
        return True

    print("libusb backend unavailable, attempting to reinstall libusb_package...")
    subprocess.run([sys.executable, "-m", "pip", "install", "--force-reinstall", "-q", "libusb_package"])

    try:
        import libusb_package
        core._BACKEND = usb.backend.libusb1.get_backend(find_library=libusb_package.find_library)
    except ImportError:
        core._BACKEND = None

    if core._BACKEND is None:
        print("Still unavailable. Try manually: pip install --force-reinstall libusb_package, then restart miasst.")
        return False
    return True


def _ensure_termux():
    ok = True

    lib_path = f"{os.environ.get('PREFIX', '')}/lib/libusb-1.0.so"
    if not os.path.exists(lib_path):
        _run(["pkg", "install", "-y", "libusb"])
        if not os.path.exists(lib_path):
            print("Could not install libusb. Try manually: pkg install libusb")
            ok = False

    if core.is_nonroot_termux() and not shutil.which("termux-usb"):
        _run(["pkg", "install", "-y", "termux-api"])
        if not shutil.which("termux-usb"):
            print("Could not install termux-api. Try manually: pkg install termux-api")
            ok = False

    return ok


def _ensure_unix():
    if usb.backend.libusb1.get_backend():
        return True

    system = platform.system()
    if system == "Darwin":
        if not shutil.which("brew"):
            print("Homebrew not found. Install libusb manually: brew install libusb")
            return False
        _run(["brew", "install", "libusb"])

    elif shutil.which("apt-get"):
        cmd = ["apt-get", "install", "-y", "libusb-1.0-0"]
        if os.geteuid() == 0:
            _run(cmd)
        elif shutil.which("sudo"):
            _run(["sudo"] + cmd)
        else:
            print("Need root to install libusb. Try manually: sudo apt-get install libusb-1.0-0")
            return False

    else:
        print("No supported package manager found. Install libusb manually for your distro.")
        return False

    return bool(usb.backend.libusb1.get_backend())


def ensure_libusb():
    if os.path.exists(_MARKER):
        return

    if platform.system() == "Windows":
        ok = _ensure_windows()
    elif os.path.exists("/data/data/com.termux"):
        ok = _ensure_termux()
    else:
        ok = _ensure_unix()

    if ok:
        open(_MARKER, "w").close()
