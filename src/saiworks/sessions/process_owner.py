"""Local Linux process identity; unavailable information never proves death."""
from pathlib import Path
import os


def _host():
    try:
        return (Path('/etc/machine-id').read_text().strip(),
                Path('/proc/sys/kernel/random/boot_id').read_text().strip())
    except OSError:
        return None


def _process(pid):
    try:
        data = Path(f'/proc/{pid}/stat').read_text()
        fields = data[data.rfind(')') + 2:].split()
        return fields[0], fields[19]
    except FileNotFoundError:
        return False
    except (OSError, IndexError):
        return None


def _namespace():
    try:
        return os.readlink('/proc/self/ns/pid')
    except OSError:
        return None


def current_owner():
    host, process, namespace = _host(), _process(os.getpid()), _namespace()
    if not host or not all(host) or not process or not namespace:
        return None
    return {'machine': host[0], 'boot': host[1], 'pid': os.getpid(), 'start': process[1], 'pid_namespace': namespace, 'uid': os.getuid()}


def owner_stopped(owner):
    host = _host()
    if not isinstance(owner, dict) or not host or not all(host) or owner.get('machine') != host[0]:
        return False
    pid = owner.get('pid')
    if (type(pid) is not int or pid <= 0 or not isinstance(owner.get('start'), str)
            or not owner['start'].isdigit() or not isinstance(owner.get('boot'), str) or not owner['boot']):
        return False
    if type(owner.get('uid')) is not int or owner['uid'] != os.getuid():
        return False
    if owner['boot'] != host[1]:
        return True
    namespace = _namespace()
    if not namespace or owner.get('pid_namespace') != namespace:
        return False
    process = _process(pid)
    return process is False or bool(process and (process[0] == 'Z' or process[1] != owner['start']))
