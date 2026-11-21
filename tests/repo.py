import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".build", "__pycache__"}


def _files(suffix):
    return sorted(p for p in ROOT.rglob("*" + suffix)
                  if not SKIP_DIRS & set(p.relative_to(ROOT).parts))


def sh_files():
    return _files(".sh")


def py_files():
    return _files(".py")


def rel(path):
    return str(path.relative_to(ROOT))


def versions():
    lines = (ROOT / "driver" / "VERSION").read_text().splitlines()
    return [v for v in lines if re.fullmatch(r"\d+\.\d+\.\d+", v)]


def patch_order():
    text = (ROOT / "driver" / "build.sh").read_text()
    m = re.search(r"PATCH_ORDER=\(\n(.*?)\n\)", text, re.S)
    assert m, "PATCH_ORDER not found in driver/build.sh"
    return m.group(1).split()


def dynfb():
    """driver/dynfb/apply.py as a module: the hook table, not a copy of it."""
    import importlib.util

    path = ROOT / "driver" / "dynfb" / "apply.py"
    spec = importlib.util.spec_from_file_location("dynfb_apply", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bash_array(name):
    """A bash array literal in driver/build.sh, either written form."""
    text = (ROOT / "driver" / "build.sh").read_text()
    m = re.search(r"^" + re.escape(name) + r"=\(\n(.*?)\n\)", text, re.S | re.M)
    if m:
        return [line.strip() for line in m.group(1).splitlines() if line.strip()]
    m = re.search(r"^" + re.escape(name) + r"=\(([^)\n]*)\)", text, re.M)
    assert m, "%s not found in driver/build.sh" % name
    return m.group(1).split()


def p2p_patch_order(mode=None):
    """The optional P2P patches: one path's list, or both when mode is None.

    The two paths are kept apart because build.sh does: RM carries one PCIe P2P
    protocol per peer pair, so no build ever applies both lists.
    """
    if mode is None:
        return (_bash_array("P2P_BAR1_PATCH_ORDER")
                + _bash_array("P2P_MAILBOX_PATCH_ORDER"))
    return _bash_array("P2P_%s_PATCH_ORDER" % mode.upper())
