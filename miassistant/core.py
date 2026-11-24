import ctypes
import json
import platform
import base64
import re
import stat
import urllib.parse
import requests
import usb.core
import usb.util
import usb.backend.libusb1
import struct
import os
import sys
import time
import hashlib
import pyaes
from functools import partial
from rsa import PublicKey

_BACKEND = None
if platform.system() == "Windows":
    try:
        import libusb_package
        _BACKEND = usb.backend.libusb1.get_backend(find_library=libusb_package.find_library)
    except ImportError:
        pass

B_INTERFACE_CLASS = 0xFF
B_INTERFACE_SUBCLASS = 0x42
B_INTERFACE_PROTOCOL = 0x1

ADB_CNXN = int.from_bytes(b"CNXN", "little")
ADB_OPEN = int.from_bytes(b"OPEN", "little")
ADB_OKAY = int.from_bytes(b"OKAY", "little")
ADB_WRTE = int.from_bytes(b"WRTE", "little")
ADB_CLSE = int.from_bytes(b"CLSE", "little")

ADB_VERSION = 0x01000001
ADB_MAX_DATA = 1024 * 1024
ADB_SIDELOAD_CHUNK_SIZE = 1024 * 64
LIBUSB_OPTION_NO_DEVICE_DISCOVERY = 2
USB_TIMEOUT_MS = 10000
LIBUSB_ERROR_NO_DEVICE = -4
LIBUSB_ERROR_NOT_FOUND = -5
LIBUSB_ERROR_TIMEOUT = -7
MAX_CONSECUTIVE_IO_ERRORS = 10
SIDELOAD_IDLE_LIMIT = 900
MAX_CONSECUTIVE_WRITE_ERRORS = 20
HTTP_TIMEOUT = (10, 30)
SYNC_IDLE_LIMIT = 30
SYNC_MAX_PATH = 1024

_banner = {}
_transport = None
_info = None


class DeviceWriteError(Exception):
    pass


class DeviceTimeout(Exception):
    pass


class DeviceDisconnected(DeviceTimeout):
    pass


class DeviceIOError(DeviceTimeout):
    pass


class EPDesc(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_uint8), ("bDescriptorType", ctypes.c_uint8),
        ("bEndpointAddress", ctypes.c_uint8), ("bmAttributes", ctypes.c_uint8),
        ("wMaxPacketSize", ctypes.c_uint16), ("bInterval", ctypes.c_uint8),
        ("bRefresh", ctypes.c_uint8), ("bSynchAddress", ctypes.c_uint8),
        ("extra", ctypes.c_void_p), ("extra_length", ctypes.c_int),
    ]


class IntfDesc(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_uint8), ("bDescriptorType", ctypes.c_uint8),
        ("bInterfaceNumber", ctypes.c_uint8), ("bAlternateSetting", ctypes.c_uint8),
        ("bNumEndpoints", ctypes.c_uint8), ("bInterfaceClass", ctypes.c_uint8),
        ("bInterfaceSubClass", ctypes.c_uint8), ("bInterfaceProtocol", ctypes.c_uint8),
        ("iInterface", ctypes.c_uint8), ("endpoint", ctypes.POINTER(EPDesc)),
        ("extra", ctypes.c_void_p), ("extra_length", ctypes.c_int),
    ]


class Intf(ctypes.Structure):
    _fields_ = [("altsetting", ctypes.POINTER(IntfDesc)), ("num_altsetting", ctypes.c_int)]


class ConfigDesc(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_uint8), ("bDescriptorType", ctypes.c_uint8),
        ("wTotalLength", ctypes.c_uint16), ("bNumInterfaces", ctypes.c_uint8),
        ("bConfigurationValue", ctypes.c_uint8), ("iConfiguration", ctypes.c_uint8),
        ("bmAttributes", ctypes.c_uint8), ("MaxPower", ctypes.c_uint8),
        ("interface", ctypes.POINTER(Intf)),
        ("extra", ctypes.c_void_p), ("extra_length", ctypes.c_int),
    ]


def connect_termux_nonroot():
    fd = os.environ.get("TERMUX_USB_FD")
    if fd is None:
        sys.exit("\n\nWithout root (termux-usb must be used)\n\n")
    fd = int(fd)

    m = usb.backend.libusb1
    lib_path = f"{os.environ.get('PREFIX', '')}/lib/libusb-1.0.so"
    lib = m._load_library(find_library=lambda _: lib_path)
    m._setup_prototypes(lib)

    lib.libusb_set_option.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.libusb_set_option.restype = ctypes.c_int
    lib.libusb_set_option(None, LIBUSB_OPTION_NO_DEVICE_DISCOVERY)

    backend = m._LibUSB(lib)

    lib.libusb_wrap_sys_device.argtypes = [ctypes.c_void_p, ctypes.c_longlong, ctypes.POINTER(ctypes.c_void_p)]
    lib.libusb_wrap_sys_device.restype = ctypes.c_int
    handle = ctypes.c_void_p()
    rc = lib.libusb_wrap_sys_device(backend.ctx, fd, ctypes.byref(handle))
    if rc != 0 or not handle.value:
        sys.exit(f"\n\ndevice is not connected, or not in mi assistant mode (libusb error {rc})\n\n")

    lib.libusb_get_device.argtypes = [ctypes.c_void_p]
    lib.libusb_get_device.restype = ctypes.c_void_p
    dev = lib.libusb_get_device(handle)

    lib.libusb_get_active_config_descriptor.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(ConfigDesc))]
    lib.libusb_get_active_config_descriptor.restype = ctypes.c_int
    cfg = ctypes.POINTER(ConfigDesc)()
    ep_out = ep_in = None
    if lib.libusb_get_active_config_descriptor(dev, ctypes.byref(cfg)) == 0:
        for i in range(cfg.contents.bNumInterfaces):
            intf = cfg.contents.interface[i]
            if intf.num_altsetting == 0:
                continue
            alt = intf.altsetting[0]
            if (alt.bInterfaceClass, alt.bInterfaceSubClass, alt.bInterfaceProtocol) != (
                B_INTERFACE_CLASS, B_INTERFACE_SUBCLASS, B_INTERFACE_PROTOCOL
            ):
                continue
            ep_out = ep_in = None
            for e in range(alt.bNumEndpoints):
                ep = alt.endpoint[e]
                if ep.bmAttributes & 0x03 != 0x02:
                    continue
                if ep.bEndpointAddress & 0x80:
                    ep_in = ep.bEndpointAddress
                else:
                    ep_out = ep.bEndpointAddress
            if ep_out is not None and ep_in is not None:
                break
        else:
            ep_out = ep_in = None
    if ep_out is None:
        sys.exit("\n\ndevice is not connected, or not in mi assistant mode\n\n")

    lib.libusb_claim_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.libusb_claim_interface.restype = ctypes.c_int
    lib.libusb_claim_interface(handle, 0)

    lib.libusb_bulk_transfer.argtypes = [
        ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_char_p,
        ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_uint,
    ]
    lib.libusb_bulk_transfer.restype = ctypes.c_int

    return {"kind": "raw", "lib": lib, "handle": handle, "ep_out": ep_out, "ep_in": ep_in, "backend": backend}


def connect_normal():
    try:
        dev_list = list(usb.core.find(find_all=True, backend=_BACKEND))
    except usb.core.USBError:
        return None

    for dev in dev_list:
        for cfg in dev:
            for intf in cfg:
                if (intf.bInterfaceClass, intf.bInterfaceSubClass, intf.bInterfaceProtocol) != (
                    B_INTERFACE_CLASS, B_INTERFACE_SUBCLASS, B_INTERFACE_PROTOCOL
                ):
                    continue
                out_ep = usb.util.find_descriptor(
                    intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT
                )
                in_ep = usb.util.find_descriptor(
                    intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN
                )
                if not out_ep or not in_ep:
                    continue
                try:
                    if dev.is_kernel_driver_active(intf.bInterfaceNumber):
                        dev.detach_kernel_driver(intf.bInterfaceNumber)
                except (NotImplementedError, usb.core.USBError):
                    pass
                dev.set_configuration(cfg.bConfigurationValue)
                usb.util.claim_interface(dev, intf.bInterfaceNumber)
                return {"kind": "pyusb", "out_ep": out_ep, "in_ep": in_ep}
    return None


def is_nonroot_termux():
    return (
        bool(os.environ.get("PREFIX"))
        and os.path.exists("/data/data/com.termux")
        and hasattr(os, "geteuid")
        and os.geteuid() != 0
    )


def connect():
    transport = connect_termux_nonroot() if is_nonroot_termux() else connect_normal()
    if not transport:
        sys.exit("\n\ndevice is not connected, or not in mi assistant mode\n\n")
    return transport


def _pyusb_error_kind(error):
    code = getattr(error, "backend_error_code", None)
    errno = getattr(error, "errno", None)
    timeout_error = getattr(usb.core, "USBTimeoutError", None)
    if code == LIBUSB_ERROR_NO_DEVICE or errno == 19:
        return "no_device"
    if (timeout_error is not None and isinstance(error, timeout_error)) or code == LIBUSB_ERROR_TIMEOUT or errno == 110:
        return "timeout"
    return "other"


def usb_write(transport, data):
    if transport["kind"] == "pyusb":
        try:
            transport["out_ep"].write(data, timeout=USB_TIMEOUT_MS)
        except usb.core.USBError as e:
            if _pyusb_error_kind(e) == "no_device":
                raise DeviceDisconnected(str(e))
            raise DeviceWriteError(str(e))
    else:
        buf = ctypes.create_string_buffer(data, len(data))
        n = ctypes.c_int(0)
        r = transport["lib"].libusb_bulk_transfer(
            transport["handle"], transport["ep_out"], buf, len(data), ctypes.byref(n), USB_TIMEOUT_MS
        )
        if r == LIBUSB_ERROR_NO_DEVICE:
            raise DeviceDisconnected("device disconnected")
        if r != 0:
            raise DeviceWriteError(f"libusb_bulk_transfer write failed (code {r})")


def usb_read(transport, size):
    if transport["kind"] == "pyusb":
        try:
            return bytes(transport["in_ep"].read(size, timeout=USB_TIMEOUT_MS))
        except usb.core.USBError as e:
            kind = _pyusb_error_kind(e)
            if kind == "no_device":
                raise DeviceDisconnected(str(e))
            if kind == "timeout":
                return b""
            raise DeviceIOError(str(e))
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_int(0)
    r = transport["lib"].libusb_bulk_transfer(
        transport["handle"], transport["ep_in"], buf, size, ctypes.byref(n), USB_TIMEOUT_MS
    )
    if r in (LIBUSB_ERROR_NO_DEVICE, LIBUSB_ERROR_NOT_FOUND):
        raise DeviceDisconnected("device disconnected")
    if r not in (0, LIBUSB_ERROR_TIMEOUT):
        raise DeviceIOError(f"libusb read failed (code {r})")
    return buf.raw[: n.value] if r == 0 else b""


def send_packet(transport, cmd, arg0, arg1, data=b""):
    header = struct.pack("<6I", cmd, arg0, arg1, len(data), 0, cmd ^ 0xFFFFFFFF)
    usb_write(transport, header)
    if data:
        usb_write(transport, data)


def read_packet(transport):
    header = usb_read(transport, 24)
    if len(header) < 24:
        raise DeviceTimeout()
    cmd, arg0, arg1, length, checksum, magic = struct.unpack("<6I", header)
    if magic != (cmd ^ 0xFFFFFFFF):
        raise DeviceIOError(f"Invalid ADB packet magic (cmd={cmd:#x})")
    data = usb_read(transport, length) if length else b""
    return cmd, arg0, arg1, data


def adb_cmd(transport, command):
    local_id = transport.get("next_id", 1)
    transport["next_id"] = local_id + 1
    send_packet(transport, ADB_OPEN, local_id, 0, command.encode() + b"\x00")

    response = None
    closed = False

    for _ in range(12):
        try:
            cmd, arg0, arg1, data = read_packet(transport)
        except DeviceTimeout:
            continue

        if arg1 != local_id:
            continue

        if cmd == ADB_CLSE:
            closed = True
            send_packet(transport, ADB_CLSE, local_id, arg0)
            break

        if cmd == ADB_OKAY:
            send_packet(transport, ADB_OKAY, arg1, arg0)
            continue

        if cmd == ADB_WRTE and data:
            response = data
            send_packet(transport, ADB_OKAY, arg1, arg0)
            continue

        if data and response is None:
            response = data

    if response is None:
        if closed:
            raise DeviceIOError(f"Channel closed before receiving response for: {command}")
        raise DeviceTimeout(f"No response received for command: {command}")

    text = response.decode(errors="ignore").rstrip("\x00")
    return text[:-1] if text.endswith("\n") else text



def start_sideload(transport, file_path, validate):

    file_size = os.path.getsize(file_path)
    service = (f"sideload-host:{file_size}:{ADB_SIDELOAD_CHUNK_SIZE}:{validate}:0").encode() + b"\x00"

    local_id = transport.get("next_id", 1)
    transport["next_id"] = local_id + 1
    send_packet(transport, ADB_OPEN, local_id, 0, service)

    total_blocks = max(1, -(-file_size // ADB_SIDELOAD_CHUNK_SIZE))
    pass1 = set()
    pass2 = set()
    phase = 1

    final_message = ""
    last_activity = time.monotonic()
    near_end = False
    last_pct = -1
    io_errors = 0

    with open(file_path, "rb") as f:
        while True:
            try:
                cmd, arg0, arg1, data = read_packet(transport)
            except DeviceDisconnected:
                sys.exit("\n\nError: the device was disconnected during the transfer.\n\n")
            except DeviceIOError as e:
                io_errors += 1
                if io_errors >= MAX_CONSECUTIVE_IO_ERRORS:
                    if near_end:
                        sys.exit(f"\n\nThe device stopped responding near the end of the installation ({e}). "
                                 "It probably left sideload mode; check the phone screen and the installed version.\n\n")
                    sys.exit(f"\n\nError: repeated USB read errors ({e}). Aborting.\n\n")
                time.sleep(0.1)
                continue
            except (DeviceTimeout, DeviceWriteError):
                idle = time.monotonic() - last_activity
                if idle > SIDELOAD_IDLE_LIMIT:
                    sys.exit(f"\n\nError: no response from the device for {int(idle)} seconds. Aborting.\n\n")
                time.sleep(0.1)
                continue
            io_errors = 0
            last_activity = time.monotonic()

            if arg1 != local_id:
                continue

            if cmd == ADB_CLSE:
                if final_message:
                    break
                if data:
                    final_message = data.decode(errors="ignore")
                break

            if len(data) > 8:
                final_message = data.decode(errors="ignore")
                break

            if cmd == ADB_OKAY:
                send_packet(transport, ADB_OKAY, arg1, arg0)
                continue

            if cmd != ADB_WRTE:
                continue

            m = re.match(r"\d+", data.decode(errors="ignore"))
            block = int(m.group()) if m else 0
            offset = block * ADB_SIDELOAD_CHUNK_SIZE
            if offset > file_size:
                break
            to_write = min(ADB_SIDELOAD_CHUNK_SIZE, file_size - offset)
            f.seek(offset)
            chunk = f.read(to_write)

            attempt = 0
            while True:
                try:
                    send_packet(transport, ADB_WRTE, arg1, arg0, chunk)
                    send_packet(transport, ADB_OKAY, arg1, arg0)
                    break
                except DeviceDisconnected:
                    sys.exit("\n\nError: the device was disconnected during the transfer.\n\n")
                except DeviceWriteError as e:
                    attempt += 1
                    if attempt >= 3:
                        sys.exit(f"\n\nError: could not send block {block} ({e}). Aborting.\n\n")
                    time.sleep(0.3)

            if block not in pass1:
                pass1.add(block)
            elif block not in pass2:
                pass2.add(block)

            if phase == 1:
                pct = int(len(pass1) * 100 / total_blocks)
                if pct != last_pct:
                    last_pct = pct
                    print(f"\r[step 1/2] Transferring in progress ... {pct}/100%", end="", flush=True)
                if len(pass1) >= total_blocks:
                    phase = 2
                    last_pct = 0
                    print("\n[step 2/2] Installing in progress ... 0/100%", end="", flush=True)
            else:
                pct = min(99, int(len(pass2) * 100 / total_blocks))
                if pct >= 99:
                    near_end = True
                if pct != last_pct:
                    last_pct = pct
                    print(f"\r[step 2/2] Installing in progress ... {pct}/100%", end="", flush=True)

    if "complete" in final_message.lower():
        step = "2/2" if phase == 2 else "1/2"
        label = "Installing" if phase == 2 else "Transferring"
        print(f"\r[step {step}] {label} in progress ... 100/100%", flush=True)                 

    print(f"\n\n{final_message}\n")
    return final_message


def get_transport():
    global _transport, ADB_SIDELOAD_CHUNK_SIZE
    if _transport is not None:
        return _transport
    try:
        transport = connect()
        send_packet(transport, ADB_CNXN, ADB_VERSION, ADB_MAX_DATA, b"host::\x00")
        for _ in range(10):
            try:
                _, _, maxdata, data = read_packet(transport)
            except DeviceDisconnected:
                raise
            except DeviceTimeout:
                continue
            if data.startswith(b"sideload::"):
                break
        else:
            sys.exit("device did not send a valid sideload banner")
    except DeviceDisconnected:
        sys.exit("\n\nError: the device was disconnected.\n\n")
    except DeviceTimeout:
        sys.exit("\n\nError: device did not respond in time. Make sure the screen is on and it is in Mi Assistant mode.\n\n")
    except DeviceWriteError as e:
        sys.exit(f"\n\nError: could not talk to the device ({e}).\n\n")
    ADB_SIDELOAD_CHUNK_SIZE = maxdata
    props = data.decode(errors="ignore").rstrip("\x00").partition("::")[2]
    _banner.update(dict(i.split("=", 1) for i in props.split(";") if "=" in i))
    _transport = transport
    return _transport


def get_info():
    global _info
    if _info is None:
        transport = get_transport()
        fields = ["sn", "region", "device", "branch", "version", "romzone",
                  "language", "codebase", "carrier", "mitoken", "recoveryversion"]
        try:
            _info = {k: adb_cmd(transport, f"get{k}:") for k in fields}
        except DeviceTimeout:
            sys.exit("\n\nError: device did not respond in time. Make sure the screen is on and it is in Mi Assistant mode.\n\n")
    return _info


def read_info():
    info = get_info()
    print(f"ro.product.model: {_banner.get('ro.product.model', 'unknown')}")
    print(f"ro.product.device: {_banner.get('ro.product.device', 'unknown')}")
    print(f"ro.product.name: {_banner.get('ro.product.name', 'unknown')}")
    print(f"\nDevice: {info['device']}")
    print(f"Version: {info['version']}")
    print(f"Serial Number: {info['sn']}")
    print(f"Codebase: {info['codebase']}")
    print(f"Branch: {info['branch']}")
    print(f"Language: {info['language']}")
    print(f"Region: {info['region']}")
    print(f"ROM Zone: {info['romzone']}\n")
    print(f"Mi Token: {info['mitoken']}")
    print(f"Carrier: {info['carrier']}")
    print(f"Recovery Version: {info['recoveryversion']}")


def _validate_request(info, md5):
    key = b"miuiotavalided11"
    iv = b"0102030405060708"

    payload = {
        "d": info["device"], "v": info["version"], "c": info["codebase"], "b": info["branch"],
        "sn": info["sn"], "l": "en-US", "f": "1",
        "options": {"zone": int(info["romzone"])},
        "pkg": md5,
    }
    encrypter = pyaes.Encrypter(pyaes.AESModeOfOperationCBC(key, iv=iv))
    enc = encrypter.feed(json.dumps(payload).encode()) + encrypter.feed()
    q = urllib.parse.quote(base64.b64encode(enc).decode())

    try:
        resp = requests.post(
            "http://update.miui.com/updates/miotaV3.php",
            data=f"q={q}&t=&s=1",
            headers={"User-Agent": "MiTunes_UserAgent_v3.0", "Content-Type": "application/x-www-form-urlencoded"},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        decrypter = pyaes.Decrypter(pyaes.AESModeOfOperationCBC(key, iv=iv))
        dec = decrypter.feed(base64.b64decode(resp.text.strip())) + decrypter.feed()
        return json.loads(dec.decode("utf-8", errors="ignore"))
    except requests.RequestException as e:
        sys.exit(f"\n\nNetwork error while contacting update.miui.com: {e}\n\n")
    except ValueError:
        sys.exit("\n\nUnexpected response from update.miui.com.\n\n")


def validate_decrypt(validate):
    PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAs3X/eMU56OEc5buvfnXe
D+9X4xKsImY7Q3Y+FY/xcdRIIvypKPjqxgqV548+Vf/H2VHeUXsMgiaeAqbmNAwP
BU0He0p0YlUEOZ9Bkn6dKniuEe9G7PeL3tx6qjnf+Y5actf7IwJBTGJefa7wt+OZ
4pNCBfgPHkTW+3VGlioP8rHbnLyZ98BdNxrvcLqJHCTblwomhVR64syqP1/eVOqE
BKeHT+IkTXFZAzEVGha0FyKgBwcET/851zW93FiiC1HYpubLSiK1crbtDVK2ho4z
NWzHCPfAfKQQn1VqXthiwxTZ25i4FYbuySwZ7xfXhqJwcLKuxN9uRCHBG8KV9V5T
7wIDAQAB
-----END PUBLIC KEY-----"""

    key = PublicKey.load_pkcs1_openssl_pem(PUBLIC_KEY_PEM)
    encrypted_bytes = base64.b64decode(validate)
    c = int.from_bytes(encrypted_bytes, 'big')
    m = pow(c, key.e, key.n)
    key_size_bytes = (key.n.bit_length() + 7) // 8
    decrypted_padded_bytes = m.to_bytes(key_size_bytes, 'big')
    if decrypted_padded_bytes[0:2] != b'\x00\x01':
        raise ValueError("Error: Invalid padding (not PKCS#1 Type 1).")
    separator_index = decrypted_padded_bytes.find(b'\x00', 2)
    if separator_index == -1:
        raise ValueError("Error: Padding separator (0x00) not found.")
    payload_bytes = decrypted_padded_bytes[separator_index + 1:]
    result = json.loads(payload_bytes.decode('utf-8').replace('}{', ','))
    return result


def file_info():

    while True:
        try:
            raw = input("Enter .zip file path: ")
        except EOFError:
            sys.exit("\n\nNo input available (stdin closed).\n\n")
        file_path = os.path.expanduser(raw.strip().strip("\"'"))
        if os.path.isfile(file_path) and file_path.lower().endswith(".zip"):
            break
        print("Invalid path or file is not a .zip. Try again.")

    print("\nCalculating checksums (MD5/SHA1) ...", flush=True)

    md5_hash = hashlib.md5()
    sha1_hash = hashlib.sha1()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            md5_hash.update(chunk)
            sha1_hash.update(chunk)
    md5_hex = md5_hash.hexdigest()
    sha1_hex = sha1_hash.hexdigest()

    print(f"MD5 : {md5_hex}")
    print(f"SHA1: {sha1_hex}\n")

    return file_path, md5_hex, sha1_hex


def extract_validate(data):
    result = {}
    seen = set()
    def walk(obj, parent=None):
        if isinstance(obj, dict):
            if 'Validate' in obj and parent:
                md5 = obj.get('md5')
                if md5 not in seen:
                    seen.add(md5)
                    result[parent] = {'md5': obj.get('md5'), 'Validate': obj['Validate'], 'Erase': obj.get('Erase')}
            for k, v in obj.items():
                walk(v, k)
        elif isinstance(obj, list):
            for item in obj:
                walk(item, parent)
    walk(data)
    return result


def confirm(message):
    print(f"\n{message}")
    if input("This cannot be undone.\nType YES to continue: ").strip() != "YES":
        print("Operation cancelled.")
        return False
    return True


def flash_rom():
    path, md5, sha1 = file_info()
    info = get_info()
    pkg = _validate_request(info, md5).get("PkgRom", {})

    if "Validate" in pkg:
        if validate_decrypt(pkg["Validate"]).get("SHA1") != sha1:
            print("SHA1 mismatch between local file and server response.")
            return
    else:
        checkv = extract_validate(_validate_request(info, ""))
        for v in checkv.values():
            if validate_decrypt(v["Validate"]).get("SHA1") != sha1:
                continue
            pkg = v
            if v["md5"] != md5:
                pkg = _validate_request(info, v["md5"]).get("PkgRom", {})
                if "Validate" not in pkg:
                    print("The server did not return a flashable package for this file.")
                    return
                print("MD5 mismatch between local file and server response.")
                if validate_decrypt(pkg["Validate"]).get("SHA1") != sha1:
                    print("SHA1 mismatch between local file and server response.")
                    return
            break
        else:
            return check_rom(checkv)

    erase = str(pkg.get("Erase"))
    if erase != "0":
        note = ("NOTICE: Data will be erased during flashing." if erase == "1"
                else "NOTICE: Could not determine whether this package erases data; assume it will.")
        if not confirm(note):
            return
    start_sideload(get_transport(), path, pkg["Validate"])


def check_rom(checkv=None):
    info = get_info()
    if checkv is None:
        checkv = extract_validate(_validate_request(info, ""))
    valid = []
    for v in checkv.values():
        pkg = _validate_request(info, v["md5"]).get("PkgRom", {})
        if "Validate" in pkg:
            valid.append((pkg["md5"], f"https://bigota.d.miui.com/{pkg['version']}/{pkg['filename']}"))
    if valid:
        print("\nROMs that can be flashed on this device:")
        for md5v, link in valid:
            print(f"\nmd5: {md5v}\ndownload_url: {link}\n")
    else:
        print("\nNo ROM can be installed on this device !.")


def run_action(command, warning=None):
    if warning and not confirm(warning):
        return
    try:
        print(adb_cmd(get_transport(), command))
    except (DeviceTimeout, DeviceWriteError) as e:
        if command.startswith(("reboot", "shutdown")):
            print(f"Command sent: {command} (connection closed as expected).")
        else:
            print(f"{command.split(':')[0]} failed: {e}")


def format_frp():
    transport = get_transport()
    print(f"MiToken: {adb_cmd(transport, 'getmitoken:')}")
    signed = input("signedMiToken: ").strip()
    print(adb_cmd(transport, f"format-frp:{signed}"))


def wipe_efs():
    print("\n[WARNING]: If you are a normal user and don't know what this means, DO NOT use this command!")
    print("Wiping EFS will permanently break your phone's network and delete your IMEI.")
    if input("Type YES only if you are an expert and know exactly what you are doing: ").strip() != "YES":
        print("Operation cancelled.")
        return
    print("Wiping EFS partition...")
    print(adb_cmd(get_transport(), "wipe-efs:"))


class _SyncStream:
    def __init__(self, transport):
        self.t = transport
        self.local_id = transport.get("next_id", 1)
        transport["next_id"] = self.local_id + 1
        self.remote_id = None
        self.closed = False
        self.buf = bytearray()

        send_packet(transport, ADB_OPEN, self.local_id, 0, b"sync:\x00")
        last = time.monotonic()
        while self.remote_id is None:
            if self.closed:
                raise DeviceIOError("the device refused the sync: service")
            if self._pump():
                last = time.monotonic()
            elif time.monotonic() - last > SYNC_IDLE_LIMIT:
                raise DeviceTimeout("timed out while opening the sync: service")

    def _pump(self):
        try:
            cmd, arg0, arg1, data = read_packet(self.t)
        except DeviceDisconnected:
            raise
        except DeviceTimeout:
            return False
        if arg1 != self.local_id:
            return False
        if cmd == ADB_OKAY:
            if self.remote_id is None:
                self.remote_id = arg0
            send_packet(self.t, ADB_OKAY, self.local_id, self.remote_id)
        elif cmd == ADB_WRTE:
            self.buf += data
            send_packet(self.t, ADB_OKAY, self.local_id, arg0)
        elif cmd == ADB_CLSE:
            self.closed = True
        return True

    def write(self, data):
        send_packet(self.t, ADB_WRTE, self.local_id, self.remote_id, data)

    def read(self, n):
        last = time.monotonic()
        while len(self.buf) < n:
            if self.closed:
                raise DeviceIOError("the device closed the channel before the reply was complete")
            if self._pump():
                last = time.monotonic()
            elif time.monotonic() - last > SYNC_IDLE_LIMIT:
                raise DeviceTimeout("timed out waiting for the device")
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out


def sync_list_v2():
    path = input("Enter remote directory to list [/cache/]: ").strip() or "/cache/"
    if not path.startswith("/"):
        path = "/" + path
    if not path.endswith("/"):
        path += "/"
    print(f"Listing directory with LIS2: {path}")

    raw_path = path.encode()
    entries, buf, status = [], bytearray(), None
    try:
        if len(raw_path) > SYNC_MAX_PATH:
            raise ValueError("path is too long")
        stream = _SyncStream(get_transport())
        stream.write(b"LIS2" + struct.pack("<I", len(raw_path)) + raw_path)
        deadline = time.monotonic() + SYNC_IDLE_LIMIT

        while status is None and time.monotonic() < deadline and not (stream.closed and not stream.buf):
            buf += stream.buf
            stream.buf.clear()
            while status is None:
                tag = bytes(buf[:4])
                if tag == b"DONE":
                    status = "ok"
                elif tag == b"FAIL":
                    status = "bad"
                elif len(buf) < 76:
                    break
                elif tag != b"DNT2":
                    status = "bad"
                else:
                    name_len = struct.unpack_from("<I", buf, 72)[0]
                    if name_len > SYNC_MAX_PATH:
                        status = "bad"
                    elif len(buf) < 76 + name_len:
                        break
                    else:
                        entries.append({
                            "name": bytes(buf[76:76 + name_len]).decode("utf-8", errors="replace"),
                            "mode": struct.unpack_from("<I", buf, 24)[0],
                            "size": struct.unpack_from("<Q", buf, 40)[0],
                            "mtime": struct.unpack_from("<Q", buf, 56)[0],
                        })
                        del buf[:76 + name_len]
            if status is None and not stream._pump():
                time.sleep(0.01)
    except (DeviceTimeout, DeviceWriteError, DeviceIOError, ValueError, OSError) as e:
        print(f"LIS2 probe failed: {e}")
        return

    if status != "ok":
        print("LIS2 returned an unrecognized response.")
        return
    if not entries:
        print("Empty directory.")
        return
    entries = [e for e in entries if e["name"] not in (".", "..")]
    if not entries:
        print("Directory contains only . and ..")
        return

    entries.sort(key=lambda e: (not stat.S_ISDIR(e["mode"]), e["name"].lower()))
    print(f"\n{'Mode':<11} {'Size':>12}  {'Modified':<16}  Name")
    print("-" * 60)
    for e in entries:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(e["mtime"])) if e["mtime"] else "-"
        print(f"{stat.filemode(e['mode']):<11} {e['size']:>12}  {when:<16}  {e['name']}")
    print(f"\nTotal: {len(entries)} entries")


def sync_pull_v2():
    remote = input("Enter remote file path (eg: /cache/recovery/last_log) : ").strip()
    if not remote:
        print("Cancelled.")
        return
    default = os.path.basename(remote.rstrip("/")) or "pulled_file"
    raw = input(f"Enter local save path [{default}]: ").strip().strip("\"'")
    local = os.path.expanduser(raw) if raw else default
    if os.path.isdir(local):
        local = os.path.join(local, default)
    print(f"Pulling {remote} -> {local}")

    tmp_path = local + ".part"
    buf = bytearray()
    total = 0
    try:
        raw_path = remote.encode()
        if len(raw_path) > SYNC_MAX_PATH:
            raise ValueError("path is too long")
        stream = _SyncStream(get_transport())
        stream.write(b"RCV2" + struct.pack("<I", len(raw_path)) + raw_path)
        stream.write(struct.pack("<II", 0, 0))
        last_activity = time.monotonic()

        with open(tmp_path, "wb") as out:
            while True:
                tag = bytes(buf[:4])
                need = 4
                if tag in (b"DATA", b"FAIL"):
                    need = 8
                    if len(buf) >= 8:
                        n = struct.unpack_from("<I", buf, 4)[0]
                        if tag == b"DATA" and n > ADB_MAX_DATA:
                            raise DeviceIOError(f"invalid RCV2 data block size ({n})")
                        need = 8 + n
                elif tag == b"DONE":
                    break
                elif len(buf) >= 4:
                    raise DeviceIOError(f"unexpected RCV2 reply: {tag!r}")

                if len(buf) < need:
                    if stream.buf:
                        buf += stream.buf
                        stream.buf.clear()
                        last_activity = time.monotonic()
                    elif stream.closed and len(buf) < 4:
                        break
                    elif not stream._pump():
                        if time.monotonic() - last_activity > SYNC_IDLE_LIMIT:
                            raise DeviceTimeout("timed out waiting for RCV2 data")
                        time.sleep(0.01)
                    continue

                if tag == b"FAIL":
                    raise DeviceIOError(f"RCV2 failed: {bytes(buf[8:need]).decode(errors='replace')}")
                out.write(buf[8:need])
                del buf[:need]
                total += need - 8
                last_activity = time.monotonic()
                print(f"\rReceived {total / 1024 / 1024:.2f} MB", end="", flush=True)

        os.replace(tmp_path, local)
        print(f"\nDone. Saved {total} bytes to {local}")
    except BaseException as e:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        if isinstance(e, (DeviceTimeout, DeviceWriteError, DeviceIOError, ValueError, OSError)):
            print(f"\nError: {e}")
        else:
            raise


ACTION_HANDLERS = {
    1: ("Read Info", read_info),
    2: ("Flash Official Recovery ROM", flash_rom),
    3: ("ROMs That Can Be Flashed", check_rom),
    4: ("Format Data", partial(run_action, "format-data:", "This will erase all user data on the device.")),
    5: ("Format Cache", partial(run_action, "format-cache:")),
    6: ("Format Storage", partial(run_action, "format-storage:", "This will erase the internal storage (photos, files).")),
    7: ("Format Data + Storage", partial(run_action, "format-data-storage:", "This will erase user data AND internal storage.")),
    8: ("Format FRP", format_frp),
    9: ("Wipe Data Storage", partial(run_action, "wipe-data-storage:", "This will wipe user data and internal storage.")),
    10: ("Wipe EFS", wipe_efs),
    11: ("Reboot", partial(run_action, "reboot:")),
    12: ("Reboot to recovery", partial(run_action, "reboot:recovery")),
    13: ("Reboot to fastboot", partial(run_action, "reboot:fastboot")),
    14: ("Reboot to bootloader", partial(run_action, "reboot:bootloader")),
    15: ("Shutdown", partial(run_action, "shutdown:")),
    16: ("Get Minidump", partial(run_action, "getminidump:")),
    17: ("List Files", sync_list_v2),
    18: ("Pull File", sync_pull_v2),
}