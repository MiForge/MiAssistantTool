import ctypes
import json
import base64
import re
import urllib.parse
import requests
import usb.core
import usb.util
import usb.backend.libusb1
import struct
import os
import hashlib
import pyaes
from rsa import PublicKey

B_INTERFACE_CLASS = 0xFF
B_INTERFACE_SUBCLASS = 0x42
B_INTERFACE_PROTOCOL = 0x1

ADB_CNXN = int.from_bytes(b"CNXN", "little")
ADB_OPEN = int.from_bytes(b"OPEN", "little")
ADB_OKAY = int.from_bytes(b"OKAY", "little")
ADB_WRTE = int.from_bytes(b"WRTE", "little")
ADB_VERSION = 0x01000001
ADB_MAX_DATA = 1024 * 1024
ADB_SIDELOAD_CHUNK_SIZE = 1024 * 64
LIBUSB_OPTION_NO_DEVICE_DISCOVERY = 2
USB_TIMEOUT_MS = 3000


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


def is_nonroot_termux():
    return (
        bool(os.environ.get("PREFIX"))
        and os.path.exists("/data/data/com.termux")
        and hasattr(os, "geteuid")
        and os.geteuid() != 0
    )


def find_adb_endpoints_raw(lib, dev):
    lib.libusb_get_active_config_descriptor.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(ConfigDesc))]
    lib.libusb_get_active_config_descriptor.restype = ctypes.c_int
    cfg = ctypes.POINTER(ConfigDesc)()
    if lib.libusb_get_active_config_descriptor(dev, ctypes.byref(cfg)) != 0:
        return None, None
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
            return ep_out, ep_in
    return None, None


def connect_termux_nonroot():
    fd = os.environ.get("TERMUX_USB_FD")
    if fd is None:
        exit("\n\nWithout root (termux-usb must be used)\n\n")
    fd = int(fd)

    m = usb.backend.libusb1
    lib_path = f"{os.environ.get('PREFIX', '')}/lib/libusb-1.0.so"
    lib = m._load_library(find_library=lambda _: lib_path)
    m._setup_prototypes(lib)

    lib.libusb_set_option.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.libusb_set_option.restype = ctypes.c_int
    lib.libusb_set_option(None, LIBUSB_OPTION_NO_DEVICE_DISCOVERY)

    backend = m._LibUSB(lib)
    ctx = backend.ctx

    lib.libusb_wrap_sys_device.argtypes = [ctypes.c_void_p, ctypes.c_longlong, ctypes.POINTER(ctypes.c_void_p)]
    lib.libusb_wrap_sys_device.restype = ctypes.c_int
    handle = ctypes.c_void_p()
    if lib.libusb_wrap_sys_device(ctx, fd, ctypes.byref(handle)) != 0 or not handle.value:
        exit("\n\ndevice is not connected, or not in mi assistant mode\n\n")

    lib.libusb_get_device.argtypes = [ctypes.c_void_p]
    lib.libusb_get_device.restype = ctypes.c_void_p
    dev = lib.libusb_get_device(handle)

    ep_out, ep_in = find_adb_endpoints_raw(lib, dev)
    if ep_out is None:
        exit("\n\ndevice is not connected, or not in mi assistant mode\n\n")

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
        dev_list = list(usb.core.find(find_all=True))
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


def connect():
    transport = connect_termux_nonroot() if is_nonroot_termux() else connect_normal()
    if not transport:
        exit("\n\ndevice is not connected, or not in mi assistant mode\n\n")
    return transport


class DeviceWriteError(Exception):
    pass


def usb_write(transport, data):
    if transport["kind"] == "pyusb":
        try:
            transport["out_ep"].write(data, timeout=USB_TIMEOUT_MS)
        except usb.core.USBError as e:
            raise DeviceWriteError(str(e))
    else:
        buf = ctypes.create_string_buffer(data, len(data))
        n = ctypes.c_int(0)
        r = transport["lib"].libusb_bulk_transfer(
            transport["handle"], transport["ep_out"], buf, len(data), ctypes.byref(n), USB_TIMEOUT_MS
        )
        if r != 0:
            raise DeviceWriteError(f"libusb_bulk_transfer write failed (code {r})")


def usb_read(transport, size):
    if transport["kind"] == "pyusb":
        try:
            return bytes(transport["in_ep"].read(size, timeout=USB_TIMEOUT_MS))
        except usb.core.USBError:
            return b""
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_int(0)
    r = transport["lib"].libusb_bulk_transfer(
        transport["handle"], transport["ep_in"], buf, size, ctypes.byref(n), USB_TIMEOUT_MS
    )
    return buf.raw[: n.value] if r == 0 else b""


def send_packet(transport, cmd, arg0, arg1, data=b""):
    header = struct.pack("<6I", cmd, arg0, arg1, len(data), 0, cmd ^ 0xFFFFFFFF)
    usb_write(transport, header)
    if data:
        usb_write(transport, data)


class DeviceTimeout(Exception):
    pass


def read_packet(transport):
    header = usb_read(transport, 24)
    if len(header) < 24:
        raise DeviceTimeout()
    cmd, arg0, arg1, length, checksum, magic = struct.unpack("<6I", header)
    data = usb_read(transport, length) if length else b""
    return cmd, arg0, arg1, data


def adb_connect(transport):
    send_packet(transport, ADB_CNXN, ADB_VERSION, ADB_MAX_DATA, b"host::\x00")
    _, _, _, data = read_packet(transport)
    if not data.startswith(b"sideload::"):
        exit("device not connected, or not in Mi Assistant mode")


def adb_cmd(transport, command):
    send_packet(transport, ADB_OPEN, 1, 0, command.encode())
    read_packet(transport)
    _, _, _, resp = read_packet(transport)
    read_packet(transport)
    text = resp.decode(errors="ignore")
    return text[:-1] if text.endswith("\n") else text


def start_sideload(transport, file_path, validate):
    file_size = os.path.getsize(file_path)
    send_packet(transport, ADB_OPEN, 1, 0, f"sideload-host:{file_size}:{ADB_SIDELOAD_CHUNK_SIZE}:{validate}:0".encode())

    total_sent = 0
    final_message = ""

    with open(file_path, "rb") as f:
        while True:
            try:
                cmd, arg0, arg1, data = read_packet(transport)
            except (DeviceTimeout, DeviceWriteError):
                continue

            if len(data) > 8:
                final_message = data.decode(errors="ignore")
                break

            if cmd == ADB_OKAY:
                send_packet(transport, ADB_OKAY, arg1, arg0)

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

            try:
                send_packet(transport, ADB_WRTE, arg1, arg0, chunk)
                send_packet(transport, ADB_OKAY, arg1, arg0)
            except DeviceWriteError:
                continue
            total_sent += to_write

            pct = min(100, int(total_sent * 100 / file_size))
            print(f"\rFlashing in progress ... {pct}/100%", end="", flush=True)

    print(f"\n\n{final_message}\n")
    return final_message


_transport = None
_connected = False
_info = None


def get_transport():
    global _transport, _connected
    if _transport is None:
        _transport = connect()
    if not _connected:
        try:
            adb_connect(_transport)
        except DeviceTimeout:
            exit("\n\nError: device did not respond in time. Make sure the screen is on and it is in Mi Assistant mode.\n\n")
        _connected = True
    return _transport


def get_info():
    global _info
    if _info is None:
        transport = get_transport()
        try:
            _info = {
                "sn": adb_cmd(transport, "getsn:"),
                "region": adb_cmd(transport, "getregion:"),
                "device": adb_cmd(transport, "getdevice:"),
                "branch": adb_cmd(transport, "getbranch:"),
                "version": adb_cmd(transport, "getversion:"),
                "romzone": adb_cmd(transport, "getromzone:"),
                "language": adb_cmd(transport, "getlanguage:"),
                "codebase": adb_cmd(transport, "getcodebase:"),
                "carrier": adb_cmd(transport, "getcarrier:"),
                "mitoken": adb_cmd(transport, "getmitoken:"),
                "recoveryversion": adb_cmd(transport, "getrecoveryversion:"),
            }
        except DeviceTimeout:
            exit("\n\nError: device did not respond in time. Make sure the screen is on and it is in Mi Assistant mode.\n\n")
    return _info



def read_info():
    info = get_info()
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


BLOCK_SIZE = 16

def _pkcs7_pad(data):
    p = BLOCK_SIZE - len(data) % BLOCK_SIZE
    return data + bytes([p]) * p

def _pkcs7_unpad(data):
    return data[:-data[-1]]

def _aes_cbc_encrypt(key, iv, data):
    mode = pyaes.AESModeOfOperationCBC(key, iv=iv)
    return b''.join(mode.encrypt(data[i:i+BLOCK_SIZE]) for i in range(0, len(data), BLOCK_SIZE))

def _aes_cbc_decrypt(key, iv, data):
    mode = pyaes.AESModeOfOperationCBC(key, iv=iv)
    return b''.join(mode.decrypt(data[i:i+BLOCK_SIZE]) for i in range(0, len(data), BLOCK_SIZE))

def _validate_request(info, md5):
    key = b"miuiotavalided11"
    iv = b"0102030405060708"

    payload = {
        "d": info["device"], "v": info["version"], "c": info["codebase"], "b": info["branch"],
        "sn": info["sn"], "l": "en-US", "f": "1",
        "options": {"zone": int(info["romzone"])},
        "pkg": md5,
    }
    body = _pkcs7_pad(json.dumps(payload).encode())
    enc = _aes_cbc_encrypt(key, iv, body)
    q = urllib.parse.quote(base64.b64encode(enc).decode())

    resp = requests.post(
        "http://update.miui.com/updates/miotaV3.php",
        data=f"q={q}&t=&s=1",
        headers={"User-Agent": "MiTunes_UserAgent_v3.0", "Content-Type": "application/x-www-form-urlencoded"},
    )
    dec = _aes_cbc_decrypt(key, iv, base64.b64decode(resp.text.strip()))
    result = json.loads(_pkcs7_unpad(dec).decode("utf-8", errors="ignore"))
    return result

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
        file_path = input("Enter .zip file path: ")
        if os.path.isfile(file_path) and file_path.lower().endswith(".zip"):
            break
        print("Invalid path or file is not a .zip. Try again.")

    md5_hash = hashlib.md5()
    sha1_hash = hashlib.sha1()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            md5_hash.update(chunk)
            sha1_hash.update(chunk)
    md5_hex = md5_hash.hexdigest()
    sha1_hex = sha1_hash.hexdigest()

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
                    result[parent] = {'md5': obj.get('md5'), 'Validate': obj['Validate']}
            for k, v in obj.items():
                walk(v, k)
        elif isinstance(obj, list):
            for item in obj:
                walk(item, parent)
    walk(data)
    return result

def flash_rom():
    path, md5, sha1 = file_info()

    info = get_info()
    transport = get_transport()
    r = _validate_request(info, md5)

    if "PkgRom" in r and "Validate" in r["PkgRom"]:
        validate = r["PkgRom"]["Validate"]
        decrypted = validate_decrypt(validate)
        if decrypted.get("SHA1") != sha1:
            print("SHA1 mismatch between local file and server response.")
            return
        if r["PkgRom"]["Erase"] == 1:
            input("NOTICE: Data will be erased during flashing.\nPress Enter to continue...")
        start_sideload(transport, path, validate)
    else:
        r2 = _validate_request(info, "")
        checkv = extract_validate(r2)
        for key, v in checkv.items():
            decrypted = validate_decrypt(v['Validate'])
            if decrypted.get("SHA1") == sha1:
                if v['md5'] != md5:
                    r3 = _validate_request(info, v['md5'])
                    if "PkgRom" in r3 and "Validate" in r3["PkgRom"]:
                        print("MD5 mismatch between local file and server response.")
                        validate3 = r3["PkgRom"]["Validate"]
                        start_sideload(transport, path, validate3)
                break
        else:
            valid = []
            for key, v in checkv.items():
                r4 = _validate_request(info, v['md5'])
                if "PkgRom" in r4 and "Validate" in r4["PkgRom"]:
                    pkg = r4["PkgRom"]
                    link = f"https://bigota.d.miui.com/{pkg['version']}/{pkg['filename']}"
                    valid.append((pkg['md5'], link))
            if valid:
                print("\nROMs that can be flashed on this device:")
                for md5v, link in valid:
                    print(f"\nmd5: {md5v}\ndownload_url: {link}\n")
            else:
                print("\nNo ROM can be installed on this device !.")


def check_rom():
    info = get_info()
    r2 = _validate_request(info, "")
    checkv = extract_validate(r2)
    valid = []
    for key, v in checkv.items():
        r4 = _validate_request(info, v['md5'])
        if "PkgRom" in r4 and "Validate" in r4["PkgRom"]:
            pkg = r4["PkgRom"]
            link = f"https://bigota.d.miui.com/{pkg['version']}/{pkg['filename']}"
            valid.append((pkg['md5'], link))
    if valid:
        print("\nROMs that can be flashed on this device:")
        for md5v, link in valid:
            print(f"\nmd5: {md5v}\ndownload_url: {link}\n")
    else:
            print("\nNo ROM can be installed on this device !.")


def send_simple(command):
    transport = get_transport()
    print(adb_cmd(transport, command))

def format_data():
    send_simple("format-data:")

def format_cache():
    send_simple("format-cache:")

def format_storage():
    send_simple("format-storage:")

def format_data_storage():
    send_simple("format-data-storage:")

def format_frp():
    send_simple("format-frp:")

def wipe_data_storage():
    send_simple("wipe-data-storage:")

def wipe_efs():
    print("\n⚠️ [WARNING]: If you are a normal user and don't know what this means, DO NOT use this command!")
    print("Wiping EFS will permanently break your phone's network and delete your IMEI.")
    confirm = input("Type 'YES' only if you are an expert and know exactly what you are doing: ")  
    if confirm == "YES":
        print("Wiping EFS partition...")
        send_simple("wipe-efs:")
    else:
        print("Operation cancelled.")


def send_disconnecting(command, verb):
    transport = get_transport()
    try:
        print(adb_cmd(transport, command))
    except DeviceTimeout:
        print(f"Device is {verb} (connection closed as expected).")


def reboot():
    send_disconnecting("reboot:", "rebooting")

def reboot_recovery():
    send_disconnecting("recovery:", "rebooting to recovery")

def reboot_fastboot():
    send_disconnecting("fastboot:", "rebooting to fastboot")

def reboot_bootloader():
    send_disconnecting("bootloader:", "rebooting to bootloader")


def shutdown():
    send_disconnecting("shutdown:", "shutting down")

ACTION_HANDLERS = {
    1: ("Read Info", read_info),
    2: ("Flash Official Recovery ROM", flash_rom),
    3: ("ROMs That Can Be Flashed", check_rom),
    4: ("Format Data", format_data),
    5: ("Format Cache", format_cache),
    6: ("Format Storage", format_storage),
    7: ("Format Data + Storage", format_data_storage),
    8: ("Format FRP", format_frp),
    9: ("Wipe Data Storage", wipe_data_storage),
    10: ("Wipe EFS", wipe_efs),
    11: ("Reboot", reboot),
    12: ("Reboot to recovery", reboot_recovery),
    13: ("Reboot to fastboot", reboot_fastboot),
    14: ("Reboot to bootloader", reboot_bootloader),
    15: ("Shutdown", shutdown),
}
