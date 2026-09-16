import os
import platform
import shutil
import subprocess
import sys

import usb.backend.libusb1

from miassistant import core

_MARKER = os.path.expanduser("~/.miasst_deps_ok")

PKGS = {
    "apt-get": "libusb-1.0-0",
    "dnf": "libusb1",
    "yum": "libusb1",
    "pacman": "libusb",
    "zypper": "libusb-1_0-0",
}

UDEV_PATH = "/etc/udev/rules.d/51-miasst.rules"
UDEV_RULES = (
    'SUBSYSTEM=="usb", ATTR{idVendor}=="2717", MODE="0666", GROUP="plugdev"\n'
    'SUBSYSTEM=="usb", ATTR{idVendor}=="05c6", MODE="0666", GROUP="plugdev"\n'
    'SUBSYSTEM=="usb", ATTR{idVendor}=="18d1", MODE="0666", GROUP="plugdev"\n'
)


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


def _install_libusb_unix():
    if platform.system() == "Darwin":
        if not shutil.which("brew"):
            print("Homebrew not found. Install libusb manually: brew install libusb")
            return False
        return _run(["brew", "install", "libusb"])

    for mgr, pkg in PKGS.items():
        if not shutil.which(mgr):
            continue
        cmd = [mgr, "-S", "--noconfirm", pkg] if mgr == "pacman" else [mgr, "install", "-y", pkg]
        if os.geteuid() != 0:
            if not shutil.which("sudo"):
                print(f"Need root to install {pkg}. Try manually: sudo {' '.join(cmd)}")
                return False
            cmd = ["sudo"] + cmd
        return _run(cmd)

    print("No supported package manager found. Install libusb manually for your distro.")
    return False


def _ensure_udev():
    if platform.system() != "Linux" or os.path.exists("/data/data/com.termux"):
        return
    if os.geteuid() != 0 and not shutil.which("sudo"):
        return

    try:
        if open(UDEV_PATH).read() == UDEV_RULES:
            return
    except OSError:
        pass

    prefix = [] if os.geteuid() == 0 else ["sudo"]
    print("Writing udev rules ->", UDEV_PATH)
    p = subprocess.Popen(prefix + ["tee", UDEV_PATH], stdin=subprocess.PIPE, text=True, stdout=subprocess.DEVNULL)
    p.communicate(UDEV_RULES)
    subprocess.run(prefix + ["udevadm", "control", "--reload-rules"])
    subprocess.run(prefix + ["udevadm", "trigger"])


def _ensure_unix():
    if not usb.backend.libusb1.get_backend():
        if not _install_libusb_unix():
            return False
        if not usb.backend.libusb1.get_backend():
            print("Installed but still not detected. Try manually and rerun miasst.")
            return False

    _ensure_udev()
    return True


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
